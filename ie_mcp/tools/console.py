"""Page instrumentation and the console tools (plan Task 12).

``instrument.js`` wraps ``onerror``, ``alert``/``confirm``/``prompt``, ``showModalDialog``,
``window.open`` (and ``console``, native ``XMLHttpRequest`` where documented) in the pages the
server drives and keeps a queue of records. Two hooks move those records to ``STORE``:

* ``instrument_hook`` (``POST_HOOKS``, driver lock held): one ``execute_script`` from the
  driver's document injects the script where it is missing, walks the same-origin frames and
  drains them;
* ``dialog_drain`` (``AFTER_CALL``, no lock): over COM, for the windows the driver cannot reach
  or must not touch while a dialog is open: modal HTML dialogs, the documents behind an open
  script dialog, an explicit ``hwnd:``/``tab:`` target and an attached tab.

Everything is switched off by ``IE_INSTRUMENT=0``. Records of a dialog that closes between two
calls are lost: nothing reads a page while no call is running.

COM lifetime: a ``ComBackend`` is made and dropped inside ``drain_target``, in the thread that
runs the hook (``protocol`` enters a COM apartment for every hook thread).
"""
import re
import threading

from ie_mcp import backends, config, js, protocol
from ie_mcp.capture.console import ConsoleStore, parse_records
from ie_mcp.capture.store import NetworkEntry
from ie_mcp.protocol import tool
from ie_mcp.session import SESSION, log
from ie_mcp.tools.windows import json_result, tool_error
from ie_mcp.win32 import windows
from ie_mcp.win32.dialogs import Refused

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
# Tools whose COM target is closed or replaced by the call: its document is gone afterwards.
NO_TARGET_DRAIN = ("ie_close_window", "ie_dialog")


class _LiveRedactor:
    """The process redactor, looked up at call time (``session.REDACTOR`` may be replaced)."""

    def redact_text(self, text):
        from ie_mcp import session
        return session.REDACTOR.redact_text(text)

    def mask(self, text):
        from ie_mcp import session
        return session.REDACTOR.mask(text)


STORE = ConsoleStore(_LiveRedactor())
# Where ``xhr`` records go as ``NetworkEntry(source="page")``. None (the default): the session's
# own CaptureStore (``SESSION.store``); a test may point it at another store.
NETWORK_STORE = None

_state_lock = threading.Lock()
INSTALLS = 0        # successful hook or drain runs of the current browser: ie_status "instrumented"
LAST_FLAGS = None   # the flags last sent to a page: ie_status "xhr_wrapped"
_INSTALL_KEY = None  # which browser INSTALLS counts (a closed or recreated session starts over)


def _session_key():
    return (SESSION.driver_pid, SESSION.driver_identity)


def page_flags():
    """The flags ``instrument.js`` reads: the console stub and the XHR wrapper.

    ``wrap_xhr`` is true when the proxy cannot see the page's requests: capture is off
    (``IE_CAPTURE=0``) or the session found the proxy blind (``capture_active is False``).
    Spike D3 = proxy, so there is no "instrument-only" case.
    """
    settings = config.SETTINGS
    wrap = not settings.capture or SESSION.capture_active is False
    return {"stub": bool(settings.console_stub), "wrap_xhr": bool(wrap)}


def _installed(flags):
    global INSTALLS, LAST_FLAGS, _INSTALL_KEY
    with _state_lock:
        key = _session_key()
        if key != _INSTALL_KEY:
            INSTALLS, _INSTALL_KEY = 0, key  # a new browser: nothing is installed in it yet
        INSTALLS += 1
        LAST_FLAGS = dict(flags)


def is_instrumented():
    """ie_status ``instrumented``: on, a browser is open and a hook or drain has worked in it."""
    with _state_lock:
        return bool(config.SETTINGS.instrument and SESSION.driver is not None
                    and INSTALLS > 0 and _INSTALL_KEY == _session_key())


# --- records ------------------------------------------------------------------------------------
_XHR = re.compile(r"(\S+) (\S+) status=(\S*) resp_len=(\S*)(?: body=(.*))?\Z", re.S)


def _network_entry(rec):
    """The ``NetworkEntry`` of an ``xhr`` record, or None when the text is not in the format."""
    match = _XHR.match(rec.text)
    if match is None:
        return None
    method, url, status, _length, body = match.groups()
    return NetworkEntry(started=rec.time, method=method, url=url,
                        status=int(status) if status.isdigit() else None,
                        req_body=(body or "").encode("utf-8"), source="page")


_OPEN = re.compile(r"open=(\d+)\Z")


def ingest_state(raw, where):
    """``(stored records, open dialogs)`` for a drained string.

    ``open dialogs`` is the sum of the ``state`` pseudo-records ``drain.js`` ends with (the
    alert/confirm/prompt calls in flight in the drained window), None when there was none.
    ``xhr`` records also become page network entries, built from the raw text:
    ``CaptureStore`` redacts them itself, and the console copy is redacted by ``ConsoleStore``.
    """
    everything = parse_records(raw, where)
    open_dialogs = None
    records = []
    for rec in everything:
        if rec.kind == "state":
            match = _OPEN.match(rec.text)
            if match:
                open_dialogs = (open_dialogs or 0) + int(match.group(1))
        else:
            records.append(rec)
    if not records:
        return [], open_dialogs
    network = NETWORK_STORE if NETWORK_STORE is not None else SESSION.store
    if network is not None:
        for rec in records:
            if rec.kind != "xhr":
                continue
            try:
                entry = _network_entry(rec)
                if entry is not None:
                    network.add(entry)
            except Exception as exc:
                log(f"could not store a page XHR record: {exc}")
    return STORE.add_many(records), open_dialogs


def ingest(raw, where):
    """Parse a drained string and store it (see ``ingest_state``); the stored records."""
    return ingest_state(raw, where)[0]


# --- the hooks ----------------------------------------------------------------------------------
def instrument_hook(spec, args, result):
    """POST_HOOKS: instrument the driver's document and drain it (one ``execute_script``)."""
    if not config.SETTINGS.instrument:
        return
    driver = SESSION.driver
    if driver is None:
        return
    flags = page_flags()
    raw = driver.execute_script(js.load("hook.js"), js.load("instrument.js"), flags)
    _installed(flags)
    ingest("" if raw is None else str(raw), "driver")


def drain_target(target):
    """Inject (if missing) and drain the document of ``target`` over COM.

    Returns the number of alert/confirm/prompt calls in flight in that document (0 or more), or
    None when the drain failed or the document reported nothing. Best effort: a busy or vanished
    window is skipped (logged), never raised.
    """
    backend = None
    try:
        backend = backends.resolve_backend(target)
        flags = page_flags()
        raw = backend.js(js.load("drain.js"), None,
                         [js.load("instrument.js"), flags["stub"], flags["wrap_xhr"]])
        _installed(flags)
        return ingest_state("" if raw is None else str(raw), target)[1]
    except Exception as exc:
        log(f"could not drain {target}: {exc}")
        return None
    finally:
        refresh = getattr(backend, "refresh", None)
        if refresh is not None:
            try:
                refresh()  # drop the cached COM document before the thread moves on
            except Exception:
                pass


def _modal():
    try:
        return list(protocol.MODAL_PROBE() or [])
    except Exception as exc:
        log(f"modal probe failed: {exc}")
        return []


def script_dialog_targets(owner_hwnd, infos):
    """The COM targets of the documents that may have raised a script dialog.

    A dialog raised inside a ``showModalDialog`` window is owned by its frame (that one
    document). A page's alert is owned by the browser's dialog helper, which says nothing about
    the page, so every page window of the session is a candidate. A dialog owned by an attached
    tab is left to the tab drain: the top window may hold other tabs of the user.
    """
    if owner_hwnd in windows.ATTACHED_TOPS:
        return []
    owner = next((w for w in infos if w.hwnd == owner_hwnd), None)
    if owner is not None and owner.kind in ("html_dialog", "main", "popup"):
        return [f"hwnd:{owner.hwnd}"]
    return [f"hwnd:{w.hwnd}" for w in infos
            if w.kind in ("main", "popup") and w.hwnd not in windows.ATTACHED_TOPS]


def _session_windows():
    try:
        return windows.session_windows()
    except Exception as exc:
        log(f"could not list the session's windows: {exc}")
        return []


def _drain_targets(spec, args):
    targets = []

    def add(target):
        if target not in targets:
            targets.append(target)

    modal = _modal()
    for window in modal:
        if window.get("kind") == "html_dialog":
            add(f"hwnd:{window['hwnd']}")
    scripts = [w for w in modal if w.get("kind") == "script_dialog"]
    if scripts:
        infos = _session_windows()
        for window in scripts:
            for target in script_dialog_targets(window.get("owner_hwnd"), infos):
                add(target)
    # An explicit COM target: whether or not the call deferred its effect (a queued COM click
    # shows its records on the next call), and POST_HOOKS never run for a COM target.
    explicit = args.get("target")
    if explicit and spec.name not in NO_TARGET_DRAIN:
        try:
            if backends.target_kind(explicit) in ("hwnd", "tab"):
                add(str(explicit))
        except ValueError:
            pass  # the tool itself reports a bad target
    default = backends.DEFAULT_TARGET
    if isinstance(default, str) and default.startswith("tab:"):
        add(default)
    return targets


def dialog_drain(spec, args, result):
    """AFTER_CALL: drain what the driver hook cannot reach (see the module docstring)."""
    if not config.SETTINGS.instrument:
        return
    for target in _drain_targets(spec, args):
        drain_target(target)


def drain_dialog_owner(owner_hwnd):
    """Drain the document(s) behind a script dialog (``ie_dialog`` calls it before it looks for
    a ``dialog-open`` record). Never raises.

    Returns how many alert/confirm/prompt calls are in flight in those documents, or None when
    there was no document to read or any drain failed (an unreadable window could be the one
    that holds the dialog).
    """
    if not config.SETTINGS.instrument:
        return None
    targets = script_dialog_targets(owner_hwnd, _session_windows())
    if not targets:
        return None
    counts = [drain_target(target) for target in targets]
    if any(count is None for count in counts):
        return None
    return sum(counts)


def register_hooks():
    """Add the two hooks to the protocol once, however often this is called."""
    if instrument_hook not in protocol.POST_HOOKS:
        protocol.POST_HOOKS.append(instrument_hook)
    if dialog_drain not in protocol.AFTER_CALL:
        protocol.AFTER_CALL.append(dialog_drain)


register_hooks()


# --- ie_dialog support ----------------------------------------------------------------------------
def record_dict(rec):
    out = {"id": rec.id, "kind": rec.kind, "level": rec.level, "time": rec.time,
           "text": rec.text, "where": rec.where}
    if rec.dialog_id:
        out["dialog_id"] = rec.dialog_id
    if rec.popup:
        out["popup"] = True
    return out


def dialog_evidence(text):
    """The ``dialog-open`` record for a dialog's text as a dict, or None (extra evidence)."""
    if not text:
        return None
    try:
        rec = STORE.find_dialog(text)
    except Exception as exc:  # evidence is extra: it must never turn an answered dialog into an error
        log(f"could not look up the dialog record: {exc}")
        return None
    return record_dict(rec) if rec else None


def taskdialog_accept_evidence(window):
    """The ``dialog-open`` record that allows accepting a TaskDialog, used up; Refused otherwise.

    A TaskDialog's message is unreadable and IE's "leave this page?" prompt has the same shape,
    so three things are required, in this order (nothing is posted unless all hold):

    1. instrumentation is on (``IE_INSTRUMENT=0``: always refused);
    2. the owner's document, drained now over COM, reports an alert/confirm/prompt in flight
       in its own wrapper (``open`` > 0): a native dialog cannot fake that. A failed drain, or
       no reachable owner document, refuses;
    3. a pending ``dialog-open`` record (at most 30 s old, its page-assigned id not closed by a
       ``dialog`` record from any route) exists; it is used up, so one record authorises one
       accept.
    """
    hwnd = window.hwnd
    ask = "; ask the user"
    if not config.SETTINGS.instrument:
        raise Refused(f"hwnd:{hwnd} is a TaskDialog and instrumentation is off (IE_INSTRUMENT=0): "
                      "its message cannot be read and it might be a 'leave this page?' prompt, "
                      "so it is not accepted blindly" + ask)
    open_dialogs = drain_dialog_owner(window.owner_hwnd)
    if open_dialogs is None:
        raise Refused(f"hwnd:{hwnd} is a TaskDialog and the page that owns it could not be read "
                      "(no reachable document, or the COM call failed), so it cannot be shown "
                      "to be a dialog the page opened itself" + ask)
    if open_dialogs <= 0:
        raise Refused(f"hwnd:{hwnd} is a TaskDialog but the page reports no alert, confirm or "
                      "prompt of its own in progress (it might be a 'leave this page?' prompt, "
                      "which is not accepted blindly)" + ask)
    rec = STORE.take_pending_dialog()
    if rec is None:
        raise Refused(f"hwnd:{hwnd} is a TaskDialog and the page has no pending dialog-open "
                      "record for it (it might be a 'leave this page?' prompt, which is not "
                      "accepted blindly)" + ask)
    return record_dict(rec)


# --- the tools --------------------------------------------------------------------------------------
def _int_arg(args, name, default, minimum):
    value = args.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum}")
    return value


def _kinds_arg(args):
    kinds = args.get("kinds")
    if kinds is None or kinds == "":
        return None
    if isinstance(kinds, str):
        kinds = [part.strip() for part in kinds.split(",")]
    if not isinstance(kinds, (list, tuple)) or not all(isinstance(k, str) for k in kinds):
        raise ValueError("kinds must be a list of strings (or a comma separated string)")
    return [k for k in kinds if k] or None


@tool(
    "ie_console",
    "Records the server's page instrumentation made: script errors (kind error), alert/confirm/"
    "prompt (dialog-open when it opens, dialog when it closed, with the result), "
    "showModalDialog calls (modal: URL, features, dialogArguments, return value), window.open "
    "(open), console output (console, only where the page has a console, or all of it with "
    "IE_CONSOLE_STUB=1) and native XHR on https pages (xhr). Returns JSON {records, "
    "next_since_id}; pass next_since_id as since_id to read only what is new. Optional "
    "kinds (a list) and limit (default 100). Records are collected after each tool call, and "
    "only from pages the server has instrumented: load-time errors of the first page are not "
    "captured, and a dialog that closes between two calls leaves no record. The text is "
    "redacted. IE_INSTRUMENT=0 turns all of this off.",
    {
        "type": "object",
        "properties": {
            "since_id": {"type": "integer", "description": "only records with a higher id (default 0)"},
            "kinds": {"type": "array", "items": {"type": "string"},
                      "description": "error, dialog-open, dialog, modal, open, console, xhr"},
            "limit": {"type": "integer", "description": f"at most this many, oldest first (default {DEFAULT_LIMIT})"},
        },
    },
    needs_driver=False,
)
def t_console(args):
    try:
        since_id = _int_arg(args, "since_id", 0, 0)
        limit = min(_int_arg(args, "limit", DEFAULT_LIMIT, 1), MAX_LIMIT)
        kinds = _kinds_arg(args)
    except ValueError as exc:
        return tool_error(str(exc))
    records = STORE.list(since_id=since_id, kinds=kinds, limit=limit)
    out = {"records": [record_dict(r) for r in records],
           "next_since_id": records[-1].id if records else since_id}
    if not config.SETTINGS.instrument:
        out["note"] = "instrumentation is off (IE_INSTRUMENT=0): nothing new is recorded"
    return json_result(out)


@tool(
    "ie_console_clear",
    "Forget every record ie_console holds (ids keep counting).",
    {"type": "object", "properties": {}},
    needs_driver=False,
)
def t_console_clear(args):
    return json_result({"cleared": STORE.clear()})
