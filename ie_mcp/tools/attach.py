"""Attach to an IE-mode tab the user opened by hand: ``ie_tabs``, ``ie_attach``, ``ie_detach``.

An attached tab keeps the user's own login and state. It is read and driven over COM only (the
backend of ``target="tab:<n>"``): ``ie_attach`` makes that the default target, so the read and
interact tools need no ``target`` while attached. The IEDriver session is not used and is never
started; the tools that need it (navigation, history, page screenshots, upload) are refused by
``protocol.run_tool`` until ``ie_detach``.

Safety (CLAUDE.md): only a tab ``windows.ie_mode_tabs`` lists can be attached, and that list
leaves out the windows of other live ie-mcp sessions. For an attached tab ``MODAL_PROBE`` sees
the windows that tab's top window owns and nothing else of the user's Edge.

State lives in ``backends`` (``attach_tab``, ``detach_tab``, ``prune_attached``): rebound, never
mutated (``ATTACHED_TOPS`` and ``ATTACHED_PIDS`` are read by other
threads while this one changes them); ``DEFAULT_TARGET`` is set last on attach and cleared first
on detach, so a tool that sees it also sees the rest.
"""
from ie_mcp import backends, config
from ie_mcp.protocol import tool
from ie_mcp.session import log
from ie_mcp.tools import console as consoletools
from ie_mcp.tools.windows import json_result, tool_error
from ie_mcp.win32 import messages, windows

# Set once ie_attach has worked: the instrumentation it put into the tab outlives ie_detach
# (the Fidelity row "Instrumentation left in an attached tab", see ie_status modifications).
USED = False

LIMITS = ("Limits: no driver-only tools while attached (navigation, history, page screenshot, "
          "upload: call ie_detach first), no proxy capture of the tab's traffic (only what the "
          "page's own instrumentation records), and a page's prompt cannot be answered (only "
          "alert and confirm windows pass the ie_dialog checks; answer a prompt yourself or "
          "ask the user). Keys, hover, window screenshots and ie_console work through COM and "
          "Win32.")


def _attach_state(tab):
    global USED
    backends.attach_tab(tab)
    USED = True


def _hwnd_arg(args):
    value = args.get("hwnd")
    if isinstance(value, str):
        value = value[4:] if value.startswith("tab:") else value
        value = int(value) if value.isdigit() else None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("hwnd must be the hwnd of a tab listed by ie_tabs")
    return value


def _probe_tab(target):
    """The tab's document URL read over COM (proof that the tab can be read); raises if not."""
    backend = backends.resolve_backend(target)
    try:
        return str(backend.js("return String(document.URL)", None, []) or "")
    finally:
        backend.refresh()  # drop the cached COM document before the thread moves on


STARTING_NOTE = ("another ie-mcp session is starting its browser: its window cannot be told "
                 "apart from a tab of the user's until it has started")


@tool(
    "ie_tabs",
    "List the IE-mode tabs the user has open in Edge windows, as JSON: hwnd (give it to "
    "ie_attach), target (\"tab:<hwnd>\"), top_hwnd, pid, title, url and attached. Only the "
    "visible tab of each window is listed (activate the tab in the browser first). Windows of "
    "other ie-mcp sessions are never listed.",
    {"type": "object", "properties": {}},
    needs_driver=False,
    budget=lambda args: 30,
)
def t_tabs(args):
    with backends.ATTACH_LOCK:
        dropped = backends.prune_attached()
        current = backends.attached_tab()
    out = [{"hwnd": t.hwnd, "target": f"tab:{t.hwnd}", "top_hwnd": t.top_hwnd, "pid": t.pid,
            "title": t.title, "url": t.url, "attached": t.hwnd == current}
           for t in windows.ie_mode_tabs()]
    notes = []
    if dropped is not None:
        notes.append(f"the attached tab {dropped} is gone: detached")
    if windows.other_sessions_starting():
        notes.append(STARTING_NOTE + "; retry in a few seconds")
    if not notes:
        return json_result(out)
    return json_result({"tabs": out, "note": "; ".join(notes)})


@tool(
    "ie_attach",
    "Attach to one of the user's IE-mode tabs (hwnd from ie_tabs) so the read and interact "
    "tools (ie_text, ie_html, ie_click, ie_fill, ie_js, ie_wait_*, ie_windows, ie_console, "
    "ie_screenshot scope=window ...) act on it without a target, over COM. The user keeps their "
    "login and page state. The tab's script instrumentation (see ie_console) is installed and "
    "stays in the tab until it is reloaded, even after ie_detach. ie_close needs ie_detach "
    "first (or force=true). " + LIMITS,
    {"type": "object", "properties": {"hwnd": {
        "type": "integer", "description": "hwnd of the tab, from ie_tabs"}},
        "required": ["hwnd"]},
    needs_driver=False,
    budget=lambda args: 30,
)
def t_attach(args):
    try:
        hwnd = _hwnd_arg(args)
    except ValueError as exc:
        return tool_error(str(exc))
    target = f"tab:{hwnd}"
    # No lock across the lookup and the COM probe: they can hang on a busy page, and the
    # protocol (run_tool's refusal, the modal probe) takes ATTACH_LOCK outside any watchdog.
    backends.prune_attached()
    if windows.other_sessions_starting():
        return tool_error("another ie-mcp session is starting its browser, so its window "
                          "cannot be told apart from the user's tabs yet; retry in a few "
                          "seconds")
    tab = next((t for t in windows.ie_mode_tabs() if t.hwnd == hwnd), None)
    if tab is None:
        return tool_error(f"hwnd {hwnd} is not an IE-mode tab that can be attached "
                          "(call ie_tabs for the list)")
    # Read the tab first: a failed attach leaves an existing attachment as it was.
    try:
        url = _probe_tab(target)
    except Exception as exc:
        return tool_error(f"cannot read tab {hwnd} over COM: {exc}")
    with backends.ATTACH_LOCK:  # only the swap is locked: it makes no blocking call
        if not (messages.is_window(tab.hwnd) and messages.is_window(tab.top_hwnd)):
            return tool_error(f"tab {hwnd} closed while it was being attached")
        _attach_state(tab)
    log(f"attached to tab {hwnd} (top window {tab.top_hwnd}, process {tab.pid})")
    # Install the instrumentation now (the AFTER_CALL drain would do it after this call), so the
    # answer says what happened. A failed drain is not fatal: the tab can be read without it.
    instrumented = config.SETTINGS.instrument and consoletools.drain_target(target) is not None
    return json_result({"ok": True, "attached": target, "top_hwnd": tab.top_hwnd,
                        "title": tab.title, "url": url, "instrumented": bool(instrumented)})


@tool(
    "ie_detach",
    "Detach from the attached tab: the tools go back to the IE driver session. The page's "
    "instrumentation stays in the tab until it is reloaded.",
    {"type": "object", "properties": {}},
    needs_driver=False,
)
def t_detach(args):
    number = backends.detach_tab()
    if number is None:
        return json_result({"ok": True, "note": "no tab was attached"})
    return json_result({"ok": True, "detached": f"tab:{number}",
                        "note": "the instrumentation stays in the tab until it is reloaded"})
