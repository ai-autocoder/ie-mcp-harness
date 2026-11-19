"""MCP stdio protocol: newline-delimited JSON-RPC 2.0, plus the @tool registry.

Tool modules register themselves with ``@tool`` when imported; ``ie_mcp.tools`` imports
all of them. Every ``tools/call`` goes through ``run_tool``, the watchdog: no tool call can
hang the server. A tool that needs the browser first takes ``session.DRIVER_LOCK`` (one such
call at a time), then runs in a daemon worker thread that the caller waits for, for at most
the call's budget. A call that is too slow is answered ``BLOCKED:`` while its worker keeps
running (a thread cannot be killed); a call that cannot get the lock is answered ``BUSY:``.
"""
import base64
import contextlib
import json
import math
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Callable, Optional

from ie_mcp import __version__, backends, config
from ie_mcp.session import DRIVER_LOCK, log
from ie_mcp.win32 import htmldoc, windows

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "ie-mcp", "version": __version__}
SERVER_INSTRUCTIONS = (
    "IE interaction mode is selected when this MCP process starts. "
    "IE_INTERACTION_MODE=background is the default and suits automation on a user's active "
    "desktop: input is native (window messages) but does not take browser focus or move the "
    "user's mouse. Prefer it for navigation, reading, form entry, and ordinary clicks. If a "
    "legacy control, hover, upload, modal, or ActiveX interaction fails in background mode, "
    "restart this MCP with IE_INTERACTION_MODE=native as the compatibility fallback; native "
    "mode takes window focus and can mix with the user's own keystrokes and mouse. "
    "IE mode is never headless: a visible Edge window is always opened. "
    "ie_open needs an http(s) URL."
)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    fn: Callable[[dict], dict]
    needs_driver: Callable[[dict], bool]
    budget: Optional[Callable[[dict], float]]
    # True when the call only queued its effect (an async click runs on the page's timer), so
    # POST_HOOKS must not run yet: neither the page nor MODAL_PROBE has seen the effect.
    # None: the call defers its effect when ``args["mode"] == "async"``.
    defers_effect: Optional[Callable[[dict], bool]] = None


# Insertion-ordered: tools/list reports tools in registration order.
REGISTRY: dict[str, ToolSpec] = {}


def tool(name, description, schema, *, needs_driver=True, budget=None, defers_effect=None):
    """Register a tool function. A bool ``needs_driver`` is wrapped as ``lambda args: value``.

    ``budget(args)`` is the tool's own idea of how long a call may take, in seconds; the
    watchdog never allows less than ``IE_TOOL_TIMEOUT`` (see ``call_budget``).
    ``defers_effect(args)`` says that this call only queued its effect (see ``ToolSpec``).
    """
    if callable(needs_driver):
        needs = needs_driver
    else:
        value = bool(needs_driver)
        needs = lambda args: value  # noqa: E731

    def register(fn):
        if name in REGISTRY:
            raise ValueError(f"tool already registered: {name}")
        REGISTRY[name] = ToolSpec(name, description, schema, fn, needs, budget, defers_effect)
        return fn

    return register


def text_result(s):
    return {"content": [{"type": "text", "text": s}]}


def image_result(png: bytes):
    b64 = base64.b64encode(png).decode("ascii")
    return {"content": [{"type": "image", "data": b64, "mimeType": "image/png"}]}


def ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def err(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def public_tools():
    return [{"name": t.name, "description": t.description, "inputSchema": t.input_schema}
            for t in REGISTRY.values()]


# --- budgets -------------------------------------------------------------------
# Lock.acquire(timeout=...) raises OverflowError above threading.TIMEOUT_MAX, and a budget
# longer than an hour is a typo, not a wish.
MAX_BUDGET = 3600.0


def number_arg(args, name, default):
    """``args[name]`` as a non-negative finite number, else ``default``.

    Budgets are computed from the caller's arguments before the tool validates them, so a
    bad value ("soon", NaN, a bool) must not crash the watchdog: the tool itself reports it.
    """
    value = args.get(name, default)
    if isinstance(value, bool):
        return float(default)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(number) or number < 0:
        return float(default)
    return number


def locator_budget(wait=0):
    """Budget for a tool that auto-waits for an element: ``timeout + wait + 15``.

    ``wait`` is the tool's own post-action pause default; ``timeout`` defaults to the 10 s
    every ``SESSION.find`` call uses.
    """
    return lambda args: number_arg(args, "timeout", 10) + number_arg(args, "wait", wait) + 15


def wait_budget(timeout, interval):
    """Budget for a polling wait tool: ``timeout + interval + 15``, with that tool's defaults."""
    return lambda args: (number_arg(args, "timeout", timeout)
                         + number_arg(args, "interval", interval) + 15)


def attach_budget(wait):
    """Budget for a tool that may have to start the browser: ``attach_retries * 75 + wait + 15``.

    One attach attempt is a 30 s attach plus two process listings of up to 20 s each, so
    75 s covers it. ``IE_ATTACH_RETRIES`` is read at call time.
    """
    return lambda args: (config.SETTINGS.attach_retries * 75
                         + number_arg(args, "wait", wait) + 15)


def call_budget(spec, args):
    """Seconds one call of ``spec`` may take: the larger of ``IE_TOOL_TIMEOUT`` and the tool's own."""
    base = config.SETTINGS.tool_timeout
    if spec.budget is not None:
        try:
            declared = float(spec.budget(args))
        except Exception as exc:
            log(f"budget of {spec.name} failed ({exc}); using IE_TOOL_TIMEOUT")
            declared = base
        if math.isfinite(declared):
            base = max(base, declared)
    return min(base, MAX_BUDGET)


# --- hooks ---------------------------------------------------------------------
# Windows that block the page (script alert, modal HTML dialog, ...) as dicts with "kind",
# "title" and "hwnd". Win32-only, no COM, so it is safe to call from any thread. Replaced by
# the window tools. The real probe lists the session's visible dialog windows; replace it in
# tests.
MODAL_PROBE: Callable[[], list] = windows.modal_probe

# fn(spec, args, result): run after a successful driver tool, in one sub-thread, while the
# driver lock is still held, unless a modal is open or the call deferred its effect
# (``ToolSpec.defers_effect``, by default ``args["mode"] == "async"``).
POST_HOOKS: list = []

# fn(spec, args, result): run after every tool call, without the driver lock, each in its own
# sub-thread.
AFTER_CALL: list = []

# Zero-argument factories of context managers. The worker, and every sub-thread a hook runs
# in, enters all of them for itself and around everything it does (COM apartments: no COM
# object may outlive, or be used from outside, the thread that obtained it).
CALL_CONTEXTS: list = []


def _register_com_context():
    """Append ``htmldoc.com_apartment`` to ``CALL_CONTEXTS`` when pywin32 imports.

    Without pywin32 the server still starts (window tools and the probe are ctypes only) and
    the COM backend reports the missing extra when it is used. Returns whether it registered.
    """
    try:
        import pythoncom  # noqa: F401
    except Exception:  # ImportError, or a broken pywin32 install: either way no COM
        return False
    if htmldoc.com_apartment not in CALL_CONTEXTS:
        CALL_CONTEXTS.append(htmldoc.com_apartment)
    return True


_register_com_context()

POST_HOOK_WAIT = 3.0   # seconds the worker waits for the POST_HOOKS thread
AFTER_CALL_WAIT = 2.0  # seconds the worker waits for each AFTER_CALL thread


def _probe():
    """The open modal windows; a failing probe means "none" (and is logged)."""
    try:
        backends.prune_attached()  # before the probe reads ATTACHED_TOPS
        return list(MODAL_PROBE() or [])
    except Exception as exc:
        log(f"modal probe failed: {exc}")
        return []


def _notice(window):
    hwnd = window.get("hwnd")
    return (f"NOTICE: {window.get('kind', 'window')} open: {window.get('title', '')} "
            f"(hwnd {hwnd}). Act in it with ie_switch_window or target=\"hwnd:{hwnd}\"; "
            f"ie_windows lists every window; ie_dialog answers a script alert or confirm.")


def _with_notices(result, windows):
    """``result`` plus one NOTICE text item per open window (a copy; ``result`` is untouched)."""
    if not windows:
        return result
    out = dict(result)
    out["content"] = list(result.get("content") or []) + [
        {"type": "text", "text": _notice(w)} for w in windows]
    return out


def _error_result(text):
    return {"content": [{"type": "text", "text": text}], "isError": True}


# --- the driver lock -------------------------------------------------------------
class _Hold:
    """The driver lock, held on behalf of one tool call and released exactly once.

    Whoever ends up finishing the call (the worker, or the POST_HOOKS sub-thread) calls
    ``release``; a second call is a no-op, so no path can release the lock twice (which would
    raise) or leave it held.
    """

    def __init__(self, name):
        self.name = name
        self.since = time.monotonic()
        self._guard = threading.Lock()
        self._released = False

    def release(self):
        with self._guard:
            if self._released:
                return
            self._released = True
        global _HOLDER
        with _HOLDER_GUARD:
            if _HOLDER is self:
                _HOLDER = None
        DRIVER_LOCK.release()


_HOLDER: Optional[_Hold] = None  # who holds DRIVER_LOCK, for the BUSY message
_HOLDER_GUARD = threading.Lock()


def _take_driver(name, timeout):
    """Acquire the driver lock (waiting up to ``timeout`` s); a ``_Hold`` or None if busy."""
    global _HOLDER
    if not DRIVER_LOCK.acquire(timeout=timeout):
        return None
    hold = _Hold(name)
    with _HOLDER_GUARD:
        _HOLDER = hold
    return hold


@contextlib.contextmanager
def driver_hold(name, timeout):
    """For a tool that takes the driver lock itself (``needs_driver=False``): ``with
    driver_hold("ie_x", 0.5) as got:`` yields whether the lock was acquired within
    ``timeout`` s. It is registered as held by ``name`` (so a BUSY answer names it) and
    released on exit, exactly once.
    """
    hold = _take_driver(name, timeout)
    try:
        yield hold is not None
    finally:
        if hold is not None:
            hold.release()


# --- the watchdog ------------------------------------------------------------------
def _seconds(value):
    return f"{value:g}"


class _Call:
    """State shared between the caller of ``run_tool`` and its worker thread."""

    def __init__(self):
        self.guard = threading.Lock()
        self.fn_finished = False   # the tool function returned (or raised)
        self.abandoned = False     # the caller gave up: the result is to be discarded
        self.fn_done = threading.Event()  # set once the tool function is over
        self.done = threading.Event()     # set once the worker is completely over
        self.result = None


def _enter_contexts(stack):
    for factory in list(CALL_CONTEXTS):
        stack.enter_context(factory())


def _run_in_subthread(label, spec, run, wait):
    """Run ``run()`` in a daemon sub-thread that enters ``CALL_CONTEXTS`` itself.

    Returns the thread, joined for at most ``wait`` seconds (it may still be running).
    Exceptions are logged and swallowed.
    """
    def body():
        try:
            with contextlib.ExitStack() as stack:
                _enter_contexts(stack)
                run()
        except Exception as exc:
            log(f"{label} for {spec.name} failed: {exc}")

    thread = threading.Thread(target=body, name=f"{label}-{spec.name}", daemon=True)
    thread.start()
    thread.join(wait)
    return thread


def _start_post_hooks(spec, args, result, hold):
    """Run POST_HOOKS in one sub-thread that releases ``hold`` when they return.

    The worker waits for it for at most ``POST_HOOK_WAIT`` s. Returns True once the thread
    owns the release; False if it could not be started (the caller then releases).
    """
    hooks = list(POST_HOOKS)

    def body():
        try:
            with contextlib.ExitStack() as stack:
                _enter_contexts(stack)
                for hook in hooks:
                    try:
                        hook(spec, args, result)
                    except Exception as exc:
                        log(f"post-call hook for {spec.name} failed: {exc}")
        except Exception as exc:
            log(f"post-call hooks for {spec.name} failed: {exc}")
        finally:
            hold.release()

    thread = threading.Thread(target=body, name=f"post-{spec.name}", daemon=True)
    try:
        thread.start()
    except Exception as exc:
        log(f"could not start the post-call hooks for {spec.name}: {exc}")
        return False
    thread.join(POST_HOOK_WAIT)
    return True


def _effect_deferred(spec, args):
    """True if this call only queued its effect, so the POST_HOOKS must wait (see ToolSpec)."""
    if spec.defers_effect is None:
        return args.get("mode") == "async"
    try:
        return bool(spec.defers_effect(args))
    except Exception as exc:
        log(f"defers_effect of {spec.name} failed ({exc}); skipping the post-call hooks")
        return True


def _work(spec, args, call, hold):
    """The worker thread: the tool, then its hooks, then the notices."""
    post_hooks_own_the_lock = False
    try:
        with contextlib.ExitStack() as stack:
            _enter_contexts(stack)
            try:
                result = spec.fn(args)
                success = True
            except BaseException as exc:
                # BaseException too (a tool calling sys.exit, say): the worker must always
                # leave an answer, or handle() would return a null result.
                log("tool error:", traceback.format_exc())
                result = _error_result(f"ERROR in {spec.name}: {str(exc) or repr(exc)}")
                success = False
            with call.guard:
                call.fn_finished = True
                abandoned = call.abandoned
            if abandoned:
                # The caller already answered BLOCKED. Nobody wants this result, and running
                # hooks for a call the agent believes failed would only add side effects.
                log(f"{spec.name} finished after its budget; result discarded")
                return
            call.fn_done.set()

            if hold is not None:
                if (success and POST_HOOKS and not _effect_deferred(spec, args)
                        and not _probe()):
                    post_hooks_own_the_lock = _start_post_hooks(spec, args, result, hold)
                if not post_hooks_own_the_lock:
                    hold.release()  # AFTER_CALL hooks and notices run without the lock
            for hook in list(AFTER_CALL):
                _run_in_subthread("after-call", spec, lambda hook=hook: hook(spec, args, result),
                                  AFTER_CALL_WAIT)
            call.result = _with_notices(result, _probe())
    except BaseException as exc:
        # Not re-raised: nobody joins this daemon thread; the caller reads call.result.
        log(f"worker for {spec.name} failed: {exc!r}")
        if call.result is None:
            call.result = _error_result(f"ERROR in {spec.name}: {str(exc) or repr(exc)}")
    finally:
        if hold is not None and not post_hooks_own_the_lock:
            hold.release()
        call.fn_done.set()
        call.done.set()


def _blocked_by_modal(windows):
    return _with_notices(_error_result(
        "BLOCKED: modal dialog open; the browser cannot be driven until it is closed. "
        "Use ie_windows to look at it, or ie_close force=true to give up on the session."),
        windows)


def _busy(spec):
    windows = _probe()
    if windows:
        return _blocked_by_modal(windows)
    with _HOLDER_GUARD:
        holder = _HOLDER
    if holder is None:
        # Released between our timeout and now: the next try will most likely succeed.
        text = "BUSY: another call has just finished with the browser; try again"
    else:
        text = (f"BUSY: {holder.name} has held the browser for "
                f"{time.monotonic() - holder.since:.0f}s; try again when it has finished, "
                f"or use ie_close force=true if it is stuck")
    return _error_result(text)


def _blocked(budget):
    return _with_notices(_error_result(
        f"BLOCKED: the browser did not answer within {_seconds(budget)}s; the action may "
        f"still complete. Use ie_windows to look for an open dialog, or ie_close force=true "
        f"to give up on the session."), _probe())


def run_tool(spec, args):
    """Run one tool call under the watchdog and return its MCP result (never raises).

    A tool that needs the driver first waits up to its budget for ``DRIVER_LOCK``; without it
    the answer is ``BUSY:`` (or ``BLOCKED:`` when a modal dialog is open, which is the usual
    reason). The tool then runs in a daemon worker; if it is still running after the budget
    the answer is ``BLOCKED:`` and the worker is abandoned: it keeps the lock until its call
    returns, then discards its result.
    """
    budget = call_budget(spec, args)
    try:
        wants_driver = bool(spec.needs_driver(args))
    except Exception:
        wants_driver = True
    if wants_driver and not args.get("target"):
        backends.prune_attached()  # a tab that is gone must not keep refusing (or hold a top)
        attached = backends.attached_tab()
        if attached is not None:
            # Before the lock and before any worker: such a call must never start a browser.
            return _with_notices(_error_result(
                f"attached to tab {attached}: this tool needs the driver session; call "
                "ie_detach first"), _probe())
    hold = None
    if wants_driver:
        hold = _take_driver(spec.name, budget)
        if hold is None:
            return _busy(spec)

    call = _Call()
    worker = threading.Thread(target=_work, args=(spec, args, call, hold),
                              name=f"tool-{spec.name}", daemon=True)
    try:
        worker.start()
    except BaseException:
        if hold is not None:
            hold.release()
        raise
    if not call.fn_done.wait(budget):
        with call.guard:
            if not call.fn_finished:
                call.abandoned = True
        if call.abandoned:
            return _blocked(budget)
    # The tool is over; its hooks and the notices are bounded by their own joins.
    finish = POST_HOOK_WAIT + AFTER_CALL_WAIT * len(AFTER_CALL) + 10
    if not call.done.wait(finish) and call.result is None:
        return _blocked(budget)
    return call.result


def handle(msg):
    """Return a response dict, or None for notifications."""
    method = msg.get("method")
    mid = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        client_ver = params.get("protocolVersion", PROTOCOL_VERSION)
        return ok(mid, {
            "protocolVersion": client_ver,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
            "instructions": SERVER_INSTRUCTIONS,
        })
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return ok(mid, {})
    if method == "tools/list":
        return ok(mid, {"tools": public_tools()})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        spec = REGISTRY.get(name)
        if spec is None:
            return err(mid, -32602, f"unknown tool: {name}")
        if not isinstance(args, dict):
            return err(mid, -32602, f"arguments of {name} must be an object")
        return ok(mid, run_tool(spec, args))
    if mid is not None:
        return err(mid, -32601, f"method not found: {method}")
    return None


def serve(stdin, stdout):
    """Read one JSON-RPC message per line from ``stdin``; write one response line to ``stdout``.

    Each ``tools/call`` is handled on its own thread, so a slow call never stops the server
    reading (a ``ping``, or a force-close, must get through). Responses are written under a
    lock and may come back out of order; each carries its request ``id``. At end of input the
    calls still running are waited for (each is bounded by the watchdog), so the caller can
    close the session without racing a running tool.
    """
    out_lock = threading.Lock()

    def answer(msg):
        try:
            resp = handle(msg)
        except Exception as exc:
            log("handler error:", traceback.format_exc())
            resp = err(msg.get("id"), -32603, f"internal error: {exc}") \
                if msg.get("id") is not None else None
        if resp is not None:
            with out_lock:
                stdout.write(json.dumps(resp) + "\n")
                stdout.flush()

    running = []
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            log(f"DROP non-JSON line ({len(line)} chars)")
            continue
        if not isinstance(msg, dict):
            log(f"DROP non-object message ({len(line)} chars)")
            continue
        log(f"recv id={msg.get('id')} method={msg.get('method')} "
            f"tool={(msg.get('params') or {}).get('name')}")
        if msg.get("method") == "tools/call":
            thread = threading.Thread(target=answer, args=(msg,), name="tools-call", daemon=True)
            thread.start()
            running = [t for t in running if t.is_alive()] + [thread]
        else:
            answer(msg)
    for thread in running:
        thread.join()
