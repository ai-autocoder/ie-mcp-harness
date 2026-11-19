"""Reaching an IE document through its ``Internet Explorer_Server`` window.

The window-finding helpers are ctypes only. Everything that needs COM imports pywin32 lazily,
so the server starts, and lists its tools, without it (``pip install "ie-mcp[win32]"`` adds it).

COM lifetime: no COM object outlives the thread that obtained it. A thread enters
``com_apartment()`` before it calls ``document_from_hwnd`` and drops every document it holds
before the apartment closes.
"""
import contextlib
import time

from ie_mcp.win32 import windows

IE_SERVER_CLASS = windows.IE_SERVER_CLASS
WM_HTML_GETOBJECT = "WM_HTML_GETOBJECT"
RPC_E_CALL_REJECTED = 0x80010001
RPC_E_SERVERCALL_RETRYLATER = 0x8001010A
RETRY_HRESULTS = (RPC_E_CALL_REJECTED, RPC_E_SERVERCALL_RETRYLATER)
RETRY_WINDOW_S = 1.0    # how long a rejected call is retried (D7)
RETRY_INTERVAL_S = 0.1
LOCALE_USER_DEFAULT = 0x0400
_NO_PYWIN32 = ('pywin32 is required to read IE documents over COM: '
               'pip install "ie-mcp[win32]"')


# --- Win32 wrappers (ctypes; tests patch these) -----------------------------------------
def window_class(hwnd):
    """The window class of ``hwnd``, ``""`` when the window is gone."""
    return windows.class_name(hwnd)


def _is_visible(hwnd):
    return windows.is_visible(hwnd)


def ie_server_descendants(hwnd):
    """Every ``Internet Explorer_Server`` descendant of ``hwnd``, hidden ones included.

    An inactive tab's server window is hidden, so a caller that wants a specific tab must
    see it too.
    """
    return [c.hwnd for c in windows.enum_descendants(hwnd) if c.cls == IE_SERVER_CLASS]


def ie_server_child(hwnd):
    """``hwnd`` itself when it is an IE server window, else its first visible IE server
    descendant, else None.

    D15: an IE server hwnd survives navigation, but a caller can still use this to find it
    again (a tab whose window was recreated, a dialog's document).
    """
    if window_class(hwnd) == IE_SERVER_CLASS:
        return hwnd
    for candidate in ie_server_descendants(hwnd):
        if _is_visible(candidate):
            return candidate
    return None


# --- COM (pywin32, imported lazily) ------------------------------------------------------
@contextlib.contextmanager
def com_apartment():
    """``CoInitialize``/``CoUninitialize`` around a thread's COM work."""
    import pythoncom
    pythoncom.CoInitialize()
    try:
        yield
    finally:
        pythoncom.CoUninitialize()


def _hresult(exc):
    """The unsigned HRESULT of a COM error, or None for any other exception."""
    code = getattr(exc, "hresult", None)
    if code is None and exc.args and isinstance(exc.args[0], int):
        # pywintypes errors and older pythoncom.com_error keep it as args[0].
        code = exc.args[0]
    return code & 0xFFFFFFFF if isinstance(code, int) else None


def com_call(fn, *args):
    """``fn(*args)``, retrying while IE rejects the call.

    Quirk (D7): a busy Trident answers ``RPC_E_CALL_REJECTED`` or ``RPC_E_SERVERCALL_RETRYLATER``
    instead of waiting. Retry every 100 ms for up to 1 s, then raise the last error. Any other
    error is raised at once.
    """
    deadline = time.monotonic() + RETRY_WINDOW_S
    while True:
        try:
            return fn(*args)
        except Exception as exc:
            if _hresult(exc) not in RETRY_HRESULTS or time.monotonic() >= deadline:
                raise
        time.sleep(RETRY_INTERVAL_S)


def document_from_hwnd(hwnd, timeout_ms=1000):
    """The COM document (a raw ``PyIDispatch``) behind an ``Internet Explorer_Server`` window.

    Asks the window for its object with ``WM_HTML_GETOBJECT`` (``SMTO_ABORTIFHUNG``, so a hung
    IE cannot block us for longer than ``timeout_ms``). Must be called inside the calling
    thread's apartment. Quirk (D7): in a documentMode 11 document win32com's dynamic dispatch
    cannot see expandos or DOM methods, so the raw dispatch is returned undecorated; use it
    through ``execScript`` and ``IDispatchEx``, never ``getattr``.
    """
    try:
        import pythoncom
        import win32con
        import win32gui
    except ImportError as exc:
        raise RuntimeError(_NO_PYWIN32) from exc
    message = win32gui.RegisterWindowMessage(WM_HTML_GETOBJECT)
    try:
        _, lresult = win32gui.SendMessageTimeout(
            hwnd, message, 0, 0, win32con.SMTO_ABORTIFHUNG, int(timeout_ms))
    except Exception as exc:  # pywintypes.error: the window is gone, hung or timed out
        raise RuntimeError(f"window {hwnd} did not answer WM_HTML_GETOBJECT: {exc}") from exc
    if not lresult:
        raise RuntimeError(f"window {hwnd} returned no document (not an IE server window, "
                           "or it is busy)")
    return com_call(pythoncom.ObjectFromLresult, lresult, pythoncom.IID_IDispatch, 0)


def document_url(hwnd):
    """The URL of the document in an IE server window (``document.URL``).

    Read with the raw ``IDispatch`` (``GetIDsOfNames`` + ``Invoke``), not dynamic dispatch (D7).
    Call it inside the thread's apartment.
    """
    document = document_from_hwnd(hwnd)  # raises the RuntimeError that names the extra
    import pythoncom
    dispid = com_call(document.GetIDsOfNames, "URL")
    if isinstance(dispid, (tuple, list)):
        dispid = dispid[0]
    return com_call(document.Invoke, dispid, LOCALE_USER_DEFAULT,
                    pythoncom.DISPATCH_PROPERTYGET, True)
