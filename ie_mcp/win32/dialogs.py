"""Proof that a ``#32770`` window is one of the session's own script dialogs.

This is the safety gate (CLAUDE.md: never click, accept or dismiss a system dialog). Every code
path that sends ``WM_COMMAND`` or ``WM_CLOSE`` to a ``#32770`` window goes through
``confirm_script_dialog`` first; nothing in this module sends anything itself except
``post_command``, and that one only takes a hwnd the caller got from the confirmation.

A ``#32770`` owned by a session window may be a system dialog the page caused (file open/save,
print, a security prompt, a credential prompt). The proof therefore has four independent parts,
all required:

1. the window is one of the session's (``enumerate_windows`` lists it as ``script_dialog``: its
   owner chain reaches a session window or an attached tab) and it is visible;
2. its IMMEDIATE owner is an ``Alternate Modal Top Most`` helper window of the session's
   processes, or an ``Internet Explorer_TridentDlgFrame`` (spike D6/Q8: that is who owns every
   real script dialog; a system dialog the page causes is typically owned by the browser's top
   window, which this rejects);
3. it is a ``#32770``;
4. the exact set of its DIRECT children (class, control ID) is one of the shapes below.

Remaining risk, accepted and recorded: an ``onbeforeunload`` "leave this page?" prompt is a
TaskDialog that may be owned by the same helper and has the same one-``DirectUIHWND`` shape as a
script dialog shown as a TaskDialog, so those two cannot be told apart from the window alone.
Task 12 requires a matching ``dialog-open`` record (the page's own instrumentation) before a
TaskDialog is accepted.
"""
import time

from ie_mcp.win32 import messages, windows

CLOSE_WAIT = 2.0         # seconds an answer waits for the dialog to disappear
CLOSE_POLL = 0.05

# Spike Q8 plus a live observation: (class, control ID) of the DIRECT children of the script
# dialog windows. Matching is on the exact set, so a #32770 that has any other control (a file
# dialog, a security prompt, an alert with an extra Edit) is never taken for one of these.
# Localised titles are not used.
#
# IE shows a script dialog in one of two forms. The first dialog of a page is the classic
# MessageBox: an alert has one button (control 2), a confirm has OK (1) and Cancel (2), both
# with the icon (Static 20) and the message (Static 65535). A later dialog of the same page,
# shown within a short time, is a TaskDialog with the "don't let this page create more
# messages" check box: one DirectUIHWND child whose buttons are not windows, so an alert and
# a confirm look the same and the message is not readable. Which form a dialog takes depends
# on what the page did before, never on whether it is an alert or a confirm.
ALERT_SHAPE = frozenset({("Button", 2), ("Static", 20), ("Static", 65535)})
CONFIRM_SHAPE = frozenset({("Button", 1), ("Button", 2), ("Static", 20), ("Static", 65535)})
TASKDIALOG_SHAPE = frozenset({("DirectUIHWND", 0)})
# A prompt has the same two forms: the check box (Button 57089, "don't let this page create more
# messages") only comes with a later dialog of the page, so the first prompt lacks it.
PROMPT_SHAPE = frozenset({("Button", 1), ("Button", 2), ("Button", 57089), ("Edit", 8133),
                          ("Static", 8132), ("Static", 65535)})
FIRST_PROMPT_SHAPE = frozenset({("Button", 1), ("Button", 2), ("Edit", 8133),
                                ("Static", 8132), ("Static", 65535)})
ALERT_MESSAGE_ID = 65535  # the Static control that holds an alert's message
# WM_COMMAND control IDs (Q8): a classic alert's only button is 2; a confirm and a TaskDialog
# answer 1 (OK, true) / 2 (Cancel, false).
IDOK, IDCANCEL = 1, 2
OWNER_CLASSES = (windows.HELPER_CLASS, windows.DIALOG_FRAME_CLASS)


class Refused(Exception):
    """A request that is refused; the message is the tool's isError text. Nothing was sent."""


def dialog_shape(children):
    """``"alert"``, ``"confirm"``, ``"taskdialog"``, ``"prompt"`` or None for the direct
    children of a ``#32770`` window."""
    got = frozenset((c.cls, c.ctrl_id) for c in children)
    for name, shape in (("alert", ALERT_SHAPE), ("confirm", CONFIRM_SHAPE),
                        ("taskdialog", TASKDIALOG_SHAPE), ("prompt", PROMPT_SHAPE),
                        ("prompt", FIRST_PROMPT_SHAPE)):
        if got == shape:
            return name
    return None


def _owned_by_a_dialog_host(window):
    """True when the immediate owner is a helper/dialog frame of the session's processes."""
    return (window.owner_cls in OWNER_CLASSES and window.owner_pid is not None
            and window.owner_pid in windows.session_window_pids())


def confirm_script_dialog(hwnd, allow_prompt=False):
    """``(WindowInfo, shape, children)`` of a window proven to be one of the session's own
    script dialogs (see the module docstring); Refused, with nothing sent, otherwise.

    A prompt passes only with ``allow_prompt`` (closing it): a prompt's answer is typed, which
    only the driver alert can do.
    """
    window = windows.find_window(hwnd)
    if window is None:
        raise Refused(f"hwnd:{hwnd} is not a confirmed script dialog "
                      "(not a window of this session)")
    if window.kind == "system_dialog":
        raise Refused(f"system dialog: ask the user (hwnd:{hwnd} is not a confirmed script "
                      "dialog (system_dialog))")
    if window.kind != "script_dialog" or window.cls != windows.SCRIPT_DIALOG_CLASS \
            or not window.visible:
        raise Refused(f"hwnd:{hwnd} is not a confirmed script dialog ({window.kind})")
    if not _owned_by_a_dialog_host(window):
        raise Refused(f"hwnd:{hwnd} is not a confirmed script dialog (script_dialog): its "
                      "owner is not the browser's dialog helper, so it may be a system "
                      "dialog; ask the user")
    children = windows.enum_children(hwnd)
    shape = dialog_shape(children)
    if shape is None:
        raise Refused(f"hwnd:{hwnd} is not a confirmed script dialog (script_dialog): its "
                      "controls match no alert or confirm window; ask the user")
    if shape == "prompt" and not allow_prompt:
        raise Refused(f"hwnd:{hwnd} is not a confirmed script dialog (script_dialog): it is a "
                      "prompt, which is answered only through the driver alert")
    return window, shape, children


def alert_message(children):
    """The message of an alert or classic confirm (its Static 65535), ``None`` if unreadable."""
    for child in children:
        if child.cls == "Static" and child.ctrl_id == ALERT_MESSAGE_ID:
            text = messages.get_text(child.hwnd)
            if text:
                return text
    return None


def post_command(hwnd, control_id):
    """Press a dialog's button: ``WM_COMMAND`` with its control ID (spike Q8).

    Only for a hwnd that ``confirm_script_dialog`` returned.
    """
    if not messages.post_message(hwnd, messages.WM_COMMAND, control_id, 0):
        raise RuntimeError(f"could not post a message to hwnd:{hwnd}")


def wait_gone(hwnd):
    """True once the window is gone (polled for ``CLOSE_WAIT`` seconds)."""
    deadline = time.monotonic() + CLOSE_WAIT
    while True:
        if not messages.is_window(hwnd):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(CLOSE_POLL)
