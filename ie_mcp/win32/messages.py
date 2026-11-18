"""The only module that sends window messages (ctypes, no pywin32, no COM).

Everything is posted, never sent, except a bounded text read: a call must not be able to hang
on a window whose thread is busy in a modal loop. Callers confirm what a window is
(``ie_mcp.tools.windows``) before they post anything to it.
"""
import ctypes
import sys
from ctypes import wintypes

WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
SMTO_ABORTIFHUNG = 0x0002
TEXT_TIMEOUT_MS = 500   # how long a text read may take before the window counts as hung
TEXT_LIMIT = 4096       # characters read from one control

_api = None


def _user32():
    """user32 with explicit prototypes (HWND/WPARAM/LPARAM are pointer-sized; see windows.py)."""
    global _api
    if _api is None and sys.platform == "win32":
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.PostMessageW.restype = wintypes.BOOL
        u.IsWindow.argtypes = [wintypes.HWND]
        u.IsWindow.restype = wintypes.BOOL
        u.SendMessageTimeoutW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, ctypes.c_void_p, wintypes.UINT,
            wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
        u.SendMessageTimeoutW.restype = ctypes.c_size_t
        _api = u
    return _api


def post_message(hwnd, msg, wparam=0, lparam=0):
    """``PostMessage``; True when the message was queued."""
    u = _user32()
    return bool(u and u.PostMessageW(hwnd, msg, wparam, lparam))


def is_window(hwnd):
    """True while ``hwnd`` still identifies a window."""
    u = _user32()
    return bool(u and u.IsWindow(hwnd))


def get_text(hwnd):
    """The text of a control (``WM_GETTEXT``), ``""`` when it has none or its thread is hung.

    ``GetWindowText`` does not read the text of another process's controls reliably; a bounded
    ``SendMessageTimeout`` does (and ``SMTO_ABORTIFHUNG`` stops it waiting on a hung thread).
    """
    u = _user32()
    if u is None:
        return ""
    buffer = ctypes.create_unicode_buffer(TEXT_LIMIT)
    result = ctypes.c_size_t(0)
    ok = u.SendMessageTimeoutW(hwnd, WM_GETTEXT, TEXT_LIMIT, ctypes.addressof(buffer),
                               SMTO_ABORTIFHUNG, TEXT_TIMEOUT_MS, ctypes.byref(result))
    return buffer.value if ok else ""
