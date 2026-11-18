"""The script-dialog tool: alert, confirm and prompt, through the driver or through the window.

Two paths (spike D2/D14):

* the driver alert (``switch_to.alert``): reaches alert, confirm and prompt, and is the only
  way to read a confirm's message or type into a prompt;
* the window path (``target="hwnd:<n>"``): posts ``WM_COMMAND`` to a script-dialog window the
  session owns. It needs no driver, so it works while the driver is blocked and for a dialog
  raised inside a ``showModalDialog`` window. It acts only on a window that
  ``ie_mcp.win32.dialogs.confirm_script_dialog`` proved to be the session's own alert or
  confirm; a system dialog is never answered.
"""
from ie_mcp import protocol
from ie_mcp.protocol import tool
from ie_mcp.session import SESSION, log
from ie_mcp.tools.windows import LOCK_WAIT, json_result, parse_hwnd, tool_error
from ie_mcp.win32 import dialogs as scriptdialogs
from ie_mcp.win32.dialogs import Refused

ACTIONS = ("accept", "dismiss", "text", "sendkeys")

# Spike D14: the driver's alert reaches a prompt (driver reach = yes). If a Windows or IE build
# ever made it unreachable, a prompt could only be answered by the user, and ie_dialog says so
# instead of calling the driver (which could block while the prompt is open).
PROMPT_DRIVER_REACH = True


@tool(
    "ie_dialog",
    "Handle a script dialog (alert/confirm/prompt). action: accept (default) | dismiss | text "
    "| sendkeys (with text, for a prompt; it also accepts). Without target it uses the driver's "
    "alert, then a script alert or confirm window of the session. With target=\"hwnd:<n>\" (a "
    "script_dialog from ie_windows or a NOTICE) it needs no driver: it presses that window's "
    "button (alert: OK; confirm: accept = OK/true, dismiss = Cancel/false) after checking that "
    "it really is the session's own alert or confirm; a confirm's message is not readable "
    "through its window, so action=text then asks the driver alert when it can. A prompt is "
    "answered only through the driver alert, so a prompt raised inside a showModalDialog window "
    "cannot be answered (ask the user). System dialogs (file, print, security prompts) are "
    "never answered: 'system dialog: ask the user'.",
    {
        "type": "object",
        "properties": {
            "action": {"type": "string", "description": "accept | dismiss | text | sendkeys"},
            "text": {"type": "string", "description": "text for sendkeys"},
            "target": {"type": "string", "description": "hwnd:<n>: the script dialog window "
                       "to answer through the window path (no driver needed)"},
        },
    },
    needs_driver=lambda args: args.get("target") is None,
)
def t_dialog(args):
    action = args.get("action") or "accept"
    if action not in ACTIONS:
        return tool_error(f"action must be one of {', '.join(ACTIONS)}")
    try:
        target = args.get("target")
        if target is not None:
            return _by_window(parse_hwnd(target), action)
        return _without_target(action, args.get("text", ""))
    except Refused as exc:
        return tool_error(str(exc))


# --- the window path ------------------------------------------------------------------------
def _by_window(hwnd, action):
    window, shape, children = scriptdialogs.confirm_script_dialog(hwnd)
    readable = shape in ("alert", "confirm")  # classic forms keep the message in a Static
    message = scriptdialogs.alert_message(children) if readable else None
    if action == "text":
        if readable:
            return json_result({"ok": True, "text": message, "kind": shape})
        return json_result({"ok": True, "text": _taskdialog_text_from_driver(), "kind": shape,
                      "note": _TASKDIALOG_NOTE})
    if action == "sendkeys":
        raise Refused(f"sendkeys needs a prompt, and hwnd:{hwnd} is an {shape}: a prompt is "
                      "answered only through the driver alert (ie_dialog without a target)")
    # Q8: a classic alert has one button (2) for both answers; a confirm and a TaskDialog:
    # 1 = OK/true, 2 = Cancel/false.
    accept = action == "accept"
    control = scriptdialogs.IDOK if (shape != "alert" and accept) else scriptdialogs.IDCANCEL
    scriptdialogs.post_command(hwnd, control)
    return json_result({"ok": True, "did": "accepted" if accept else "dismissed", "kind": shape,
                  "text": message, "closed": scriptdialogs.wait_gone(hwnd)})


_TASKDIALOG_NOTE = ("the message of a dialog shown as a TaskDialog is not readable through its "
                    "window; text is only given when the driver alert could be asked")


def _taskdialog_text_from_driver():
    """The driver alert's text, only when it surely belongs to the one dialog that is open.

    ``None`` (not an invented text) when the driver is unavailable, busy, or several script
    dialogs are open, so the alert it would read might be another one.
    """
    if SESSION.driver is None:
        return None
    try:
        modal = list(protocol.MODAL_PROBE() or [])
        open_dialogs = [w for w in modal if w.get("kind") == "script_dialog"]
    except Exception as exc:
        log(f"modal probe failed: {exc}")
        return None
    if len(open_dialogs) != 1:
        return None
    if any(w.get("kind") == "html_dialog" for w in modal):
        # A driver call can block while a modal HTML dialog is open, and this one would hold
        # DRIVER_LOCK while it did. The text is simply not readable then.
        return None
    with protocol.driver_hold("ie_dialog", LOCK_WAIT) as got:
        if not got:
            return None
        try:
            return SESSION.driver.switch_to.alert.text
        except Exception as exc:
            log(f"driver alert text unavailable: {exc}")
            return None


# --- no target --------------------------------------------------------------------------------
def _without_target(action, text):
    try:
        modal = list(protocol.MODAL_PROBE() or [])
    except Exception as exc:
        log(f"modal probe failed: {exc}")
        modal = []
    html_dialog_open = any(w.get("kind") == "html_dialog" for w in modal)
    scripts = [w for w in modal if w.get("kind") == "script_dialog"]
    prompts = [w for w in scripts if w.get("is_prompt")]
    answerable = [w for w in scripts if not w.get("is_prompt")]

    # While a modal HTML dialog is open a driver call can block, so the driver alert is skipped
    # (a prompt is a script_dialog too, but only the driver alert can answer it). A prompt is
    # also never sent to the driver when D14 says the driver cannot reach it.
    skip_driver = html_dialog_open or (prompts and not PROMPT_DRIVER_REACH)
    if not skip_driver:
        driver = SESSION.driver
        if driver is None:
            return json_result({"ok": False, "note": "no dialog present: no browser session"})
        try:
            alert = driver.switch_to.alert
        except Exception as exc:
            alert = None
            note = f"no dialog present: {str(exc).splitlines()[0] if str(exc) else exc!r}"
        if alert is not None:
            return _driver_alert(alert, action, text)
        if not answerable and not prompts:
            return json_result({"ok": False, "note": note})
    if len(answerable) > 1:
        raise Refused("several script dialogs are open (" + ", ".join(
            f"hwnd:{w['hwnd']}" for w in answerable) + "): pick one with target")
    if answerable:
        return _by_window(answerable[0]["hwnd"], action)
    if prompts:
        raise Refused("prompt not reachable: ask the user")
    return json_result({"ok": False, "note": "no dialog present"})


def _driver_alert(al, action, text):
    try:
        if action == "text":
            return json_result({"ok": True, "text": al.text})
        if action == "sendkeys":
            al.send_keys(text)
            al.accept()
            return json_result({"ok": True, "did": "sent+accepted"})
        if action == "dismiss":
            txt = al.text
            al.dismiss()
            return json_result({"ok": True, "did": "dismissed", "text": txt})
        txt = al.text
        al.accept()
        return json_result({"ok": True, "did": "accepted", "text": txt})
    except Exception as exc:
        return json_result({"ok": False, "note": str(exc).splitlines()[0] if str(exc) else repr(exc)})
