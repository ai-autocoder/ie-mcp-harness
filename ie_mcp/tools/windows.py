"""Window tools: ``ie_windows``, ``ie_switch_window`` and ``ie_close_window``.

Two ways reach a window (spike D2 = both): the driver (``window_handles``, ``switch_to.window``)
and COM through the window's ``hwnd`` (``target="hwnd:<n>"`` on the other tools). ``ie_windows``
lists every window of the session with its ``hwnd`` target, and the driver handle when it can
tell which handle is which.

Safety (CLAUDE.md): a window message is only ever sent to a window that
``ie_mcp.win32.dialogs.confirm_script_dialog`` has just proven to be one of the session's own
script dialogs; a system dialog is never answered or closed.
"""
import json
import re
import time

from ie_mcp import backends, protocol
from ie_mcp.protocol import driver_hold, text_result, tool
from ie_mcp.session import SESSION, log
from ie_mcp.win32 import dialogs as scriptdialogs
from ie_mcp.win32 import messages, windows
from ie_mcp.win32.dialogs import Refused

HWND_TARGET = re.compile(r"hwnd:([0-9]+)")
PAGE_KINDS = ("main", "popup", "html_dialog")
DIALOG_KINDS = ("html_dialog", "script_dialog", "system_dialog")
LOCK_WAIT = 0.5          # seconds ie_windows waits for the driver lock before it omits the driver fields
# Quirk (seen live): IEDriver lists a new pop-up or showModalDialog window in window_handles
# about a second after the window exists (the first read can still show one handle, later ones
# two). A selector that matches nothing is therefore retried for this long before it is an error.
SWITCH_WAIT = 5.0
SWITCH_POLL = 0.5


def tool_error(text):
    return {"content": [{"type": "text", "text": text}], "isError": True}


def json_result(value):
    return text_result(json.dumps(value))


def parse_hwnd(target):
    """The hwnd of ``"hwnd:<n>"``; Refused for any other form (nothing has been touched yet)."""
    match = HWND_TARGET.fullmatch(target) if isinstance(target, str) else None
    if match is None or int(match.group(1)) <= 0:
        raise Refused(f"target must be hwnd:<n> (a window from ie_windows), not {target!r}")
    return int(match.group(1))


# --- ie_windows -------------------------------------------------------------------------------
def read_handles(driver):
    """``(original, [(handle, title, url)])`` for every driver window.

    Switches to each handle to read it (the driver has no other way) and always switches back
    to the window it started on. A handle that cannot be read has ``None`` title and URL.
    """
    handles = list(driver.window_handles)
    try:
        original = driver.current_window_handle
    except Exception:
        original = None
    at = original
    rows = []
    try:
        for handle in handles:
            try:
                if handle != at:
                    driver.switch_to.window(handle)
                    at = handle
                rows.append((handle, driver.title, driver.current_url))
            except Exception as exc:
                log(f"could not read driver window {handle}: {exc}")
                rows.append((handle, None, None))
    finally:
        if original in handles and at != original:
            driver.switch_to.window(original)
    return original, rows


def match_handles(rows, infos):
    """{driver handle: hwnd} for the handles that match exactly one page window.

    A window matches a handle when its title starts with the driver's title (the top window's
    caption adds the browser's name after the document title); more than one candidate is
    narrowed by URL. An ambiguous handle, or two handles that claim one window, stay unmatched.
    """
    pages = [w for w in infos if w.kind in PAGE_KINDS]
    claims = {}
    for handle, title, url in rows:
        if not title:
            continue
        candidates = [w for w in pages if w.title.startswith(title)]
        if len(candidates) > 1 and url:
            candidates = [w for w in candidates if w.url == url] or candidates
        if len(candidates) == 1:
            claims.setdefault(candidates[0].hwnd, []).append(handle)
    return {handles[0]: hwnd for hwnd, handles in claims.items() if len(handles) == 1}


def main_handle(rows, infos):
    """The driver handle of the session's main window, or None when it cannot be told.

    A sole handle is the main window. Otherwise the handle must match ``SESSION.main_hwnd``
    (``match_handles``); guessing (say, the first handle) could make a caller close the real
    main window or refuse the real pop-up.
    """
    if len(rows) == 1:
        return rows[0][0]
    main_hwnd = SESSION.main_hwnd
    if main_hwnd is not None:
        for handle, hwnd in match_handles(rows, infos).items():
            if hwnd == main_hwnd:
                return handle
    return None


@tool(
    "ie_windows",
    "List the windows of the session as JSON: hwnd, kind (main | popup | html_dialog | "
    "script_dialog | system_dialog), class, title, url, owner_hwnd, visible, is_prompt and "
    "target (\"hwnd:<n>\", for the target argument of the other tools). When no modal dialog "
    "is open and the browser is free it also gives each page window's driver_handle (for "
    "ie_switch_window) and which one is current. Never waits for a busy browser and works "
    "while a dialog is open. A system dialog is listed so you can tell the user: it is never "
    "answered by these tools.",
    {"type": "object", "properties": {}},
    needs_driver=False,
    budget=lambda args: 30,
)
def t_windows(args):
    infos = [w for w in windows.session_windows(with_url=True)
             if w.kind != "other" and (w.visible or w.kind in PAGE_KINDS)]
    items = []
    for w in infos:
        item = {"hwnd": w.hwnd, "target": f"hwnd:{w.hwnd}", "kind": w.kind, "cls": w.cls,
                "title": w.title, "url": w.url, "pid": w.pid, "owner_hwnd": w.owner_hwnd,
                "visible": w.visible, "is_prompt": w.is_prompt}
        items.append(item)
    # The driver fields need the browser, which a modal dialog can block: only ask when none
    # is open, and only when the lock is free (a busy lock simply omits them).
    if SESSION.driver is not None and not _modal_open():
        _add_driver_fields(items, infos)
    return json_result(items)


def _modal_open():
    try:
        return bool(protocol.MODAL_PROBE())
    except Exception as exc:
        log(f"modal probe failed: {exc}")
        return False


def _add_driver_fields(items, infos):
    with driver_hold("ie_windows", LOCK_WAIT) as got:
        if not got:
            return
        driver = SESSION.driver
        try:
            original, rows = read_handles(driver)
            matches = match_handles(rows, infos)
        except Exception as exc:
            log(f"ie_windows: could not match driver handles: {exc}")
            return
    by_hwnd = {item["hwnd"]: item for item in items}
    for handle, hwnd in matches.items():
        item = by_hwnd.get(hwnd)
        if item is not None:
            item["driver_handle"] = handle
            item["current"] = handle == original


# --- ie_switch_window -------------------------------------------------------------------------
@tool(
    "ie_switch_window",
    "Make a driver window the one the other tools act on (a pop-up from window.open, or a "
    "showModalDialog window). Give exactly one of: handle (a driver_handle from ie_windows), "
    "title_contains (case-insensitive part of the window's document title) or main=true (the "
    "session's first window). Returns the new window's title and URL. Switch back with "
    "main=true when you are done: after a window closes the driver has no current window. "
    "Alternative that needs no switching: pass target=\"hwnd:<n>\" to the other tools.",
    {
        "type": "object",
        "properties": {
            "handle": {"type": "string"},
            "title_contains": {"type": "string"},
            "main": {"type": "boolean"},
        },
    },
)
def t_switch_window(args):
    handle, part, main = args.get("handle"), args.get("title_contains"), bool(args.get("main"))
    if sum([bool(handle), bool(part), main]) != 1:
        return tool_error("give exactly one of handle, title_contains or main")
    driver = SESSION.driver
    if driver is None:
        return tool_error("no browser session: call ie_open first")
    deadline = time.monotonic() + SWITCH_WAIT
    while True:
        chosen = None
        if handle:
            if handle in driver.window_handles:
                chosen = handle
        else:
            _, rows = read_handles(driver)
            if main:
                chosen = main_handle(rows, _page_infos())
            else:
                wanted = part.lower()
                chosen = next((h for h, title, _ in rows if title and wanted in title.lower()),
                              None)
        if chosen is not None:
            break
        if time.monotonic() >= deadline:
            if handle:
                return tool_error(f"no driver window with handle {handle!r}; see ie_windows")
            if main:
                return tool_error("cannot tell which driver window is the main window: use "
                                  "handle (a driver_handle from ie_windows)")
            return tool_error(f"no driver window has a title containing {part!r}")
        time.sleep(SWITCH_POLL)
    driver.switch_to.window(chosen)
    return json_result({"ok": True, "handle": chosen, "title": driver.title,
                        "url": driver.current_url})


def _page_infos():
    try:
        return windows.session_windows(with_url=True)
    except Exception as exc:
        log(f"could not list the session windows: {exc}")
        return []


# --- ie_close_window --------------------------------------------------------------------------
def _needs_driver(args):
    """Only a driver handle needs the driver; ``hwnd:<n>`` goes through COM or Win32."""
    target = args.get("target")
    return not (isinstance(target, str) and HWND_TARGET.fullmatch(target))


@tool(
    "ie_close_window",
    "Close a pop-up or modal dialog window: target is hwnd:<n> (from ie_windows) or a "
    "driver_handle. It will not close the main window (use ie_close) or an attached tab (use "
    "ie_detach), and it never touches a system dialog (file, print or security prompt): those "
    "are reported as 'system dialog: ask the user'. A script alert/confirm/prompt is closed "
    "like its Cancel/OK button (a prompt then returns null). An hwnd target is closed with "
    "window.close() over COM, else with WM_CLOSE.",
    {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]},
    needs_driver=_needs_driver,
)
def t_close_window(args):
    target = args.get("target")
    if not isinstance(target, str) or not target:
        return tool_error("target is required: hwnd:<n> or a driver handle from ie_windows")
    try:
        match = HWND_TARGET.fullmatch(target)
        if match:
            return _close_hwnd(int(match.group(1)))
        return _close_handle(target)
    except Refused as exc:
        return tool_error(str(exc))


def _close_hwnd(hwnd):
    if hwnd in windows.ATTACHED_TOPS:
        return tool_error(f"hwnd:{hwnd} is the top window of an attached tab: use ie_detach")
    if hwnd == SESSION.main_hwnd:
        return tool_error("main window: use ie_close")
    window = windows.find_window(hwnd)
    if window is None:
        return tool_error(f"hwnd:{hwnd} is not a window of this session")
    if window.kind == "main":
        return tool_error("main window: use ie_close")
    if window.kind == "system_dialog":
        return tool_error("system dialog: ask the user")
    if window.kind == "script_dialog":
        # Cancel (2) is also what the dialog's close box does, and what a prompt needs.
        scriptdialogs.confirm_script_dialog(hwnd, allow_prompt=True)
        scriptdialogs.post_command(hwnd, scriptdialogs.IDCANCEL)
        return json_result({"ok": True, "via": "WM_COMMAND",
                            "closed": scriptdialogs.wait_gone(hwnd)})
    if window.kind not in ("popup", "html_dialog"):
        return tool_error(f"hwnd:{hwnd} is not a pop-up or dialog window ({window.kind})")
    if window.kind == "popup" and SESSION.main_hwnd is None:
        # Without a known main window, the main window itself classifies as a pop-up (R5).
        return tool_error("cannot tell which is the main window, so no page window is closed "
                          "this way (use ie_close, or close the pop-up from its page)")
    via = "com"
    try:
        backends.resolve_backend(f"hwnd:{hwnd}").close_window()
    except Exception as exc:
        # No document to ask (a window without an IE server, a rejected call, no pywin32).
        if not messages.is_window(hwnd):
            # The window was destroyed while the call ran (the close worked, then COM lost it):
            # there is nothing left to post to, and the job is done.
            log(f"hwnd:{hwnd} closed while window.close() was running ({exc})")
            return json_result({"ok": True, "via": via, "closed": True})
        log(f"window.close() over COM failed for hwnd:{hwnd} ({exc}); posting WM_CLOSE")
        via = "WM_CLOSE"
        if not messages.post_message(hwnd, messages.WM_CLOSE, 0, 0):
            return tool_error(f"could not close hwnd:{hwnd}")
    return json_result({"ok": True, "via": via, "closed": scriptdialogs.wait_gone(hwnd)})


def _close_handle(handle):
    driver = SESSION.driver
    if driver is None:
        return tool_error("no browser session: call ie_open first")
    if handle not in driver.window_handles:
        return tool_error(f"no driver window with handle {handle!r} (use hwnd:<n> or a "
                          "driver_handle from ie_windows)")
    _, rows = read_handles(driver)
    main = main_handle(rows, _page_infos())
    if main is None:
        return tool_error("cannot tell which driver handle is the main window, so nothing "
                          "was closed: use ie_close_window with hwnd:<n> from ie_windows")
    if handle == main:
        return tool_error("main window: use ie_close")
    driver.switch_to.window(handle)
    driver.close()
    remaining = driver.window_handles
    if main in remaining:
        driver.switch_to.window(main)
    return json_result({"ok": True, "via": "driver", "closed": handle, "current": main})
