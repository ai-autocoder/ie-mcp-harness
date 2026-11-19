"""Window capture through ``PrintWindow`` (ctypes only: no pywin32, no Pillow, no COM).

``PrintWindow`` asks a window to paint itself into a device context we own, so it works for a
window that is covered or on another monitor, and it never moves, focuses, clicks or sends
input to anything. Callers decide *which* windows may be captured (the session's own, never a
system dialog); this module only turns an hwnd into pixels.
"""
import ctypes
import sys
from ctypes import wintypes
from types import SimpleNamespace

PW_RENDERFULLCONTENT = 2  # also paint DirectComposition/Trident child content
DIB_RGB_COLORS = 0
BI_RGB = 0
BYTES_PER_PIXEL = 4
# A guard against a bogus rectangle: 64M pixels is 256 MB of RGBA, more than any real window.
MAX_PIXELS = 64_000_000
# SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2): the constant is -4,
# a pseudo-handle, not a pointer.
DPI_AWARENESS_PER_MONITOR_V2 = -4


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 1)]


_api = None


def _default_api():
    """``user32`` and ``gdi32`` with explicit prototypes, or None off Windows.

    Quirk: ctypes defaults every argument and result to a 32-bit ``int``; HWND, HDC and GDI
    handles are pointer-sized, so on 64-bit Python a default-prototyped call truncates them.
    """
    global _api
    if _api is None and sys.platform == "win32":
        u = ctypes.WinDLL("user32", use_last_error=True)
        g = ctypes.WinDLL("gdi32", use_last_error=True)
        handle = ctypes.c_void_p
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u.GetWindowRect.restype = wintypes.BOOL
        u.GetDC.argtypes = [wintypes.HWND]
        u.GetDC.restype = handle
        u.ReleaseDC.argtypes = [wintypes.HWND, handle]
        u.ReleaseDC.restype = ctypes.c_int
        u.PrintWindow.argtypes = [wintypes.HWND, handle, wintypes.UINT]
        u.PrintWindow.restype = wintypes.BOOL
        u.IsIconic.argtypes = [wintypes.HWND]
        u.IsIconic.restype = wintypes.BOOL
        g.CreateCompatibleDC.argtypes = [handle]
        g.CreateCompatibleDC.restype = handle
        g.CreateDIBSection.argtypes = [handle, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
                                       ctypes.POINTER(ctypes.c_void_p), handle, wintypes.DWORD]
        g.CreateDIBSection.restype = handle
        g.SelectObject.argtypes = [handle, handle]
        g.SelectObject.restype = handle
        g.DeleteObject.argtypes = [handle]
        g.DeleteObject.restype = wintypes.BOOL
        g.DeleteDC.argtypes = [handle]
        g.DeleteDC.restype = wintypes.BOOL
        _api = SimpleNamespace(user32=u, gdi32=g)
    return _api


def _log(message):
    from ie_mcp.session import log  # lazy: session imports the win32 package
    log(message)


def enable_dpi_awareness(user32=None):
    """Make this process per-monitor DPI aware (v2); True on success, False (logged) otherwise.

    Without it ``GetWindowRect`` reports scaled sizes on a scaled display while
    ``PrintWindow`` paints physical pixels, so the two disagree. It must run before the
    process creates a window or calls a window function, and it can only be set once: a second
    call, or a process whose manifest already chose, fails with access denied (False, harmless).
    """
    try:
        user32 = user32 or ctypes.WinDLL("user32", use_last_error=True)
        function = user32.SetProcessDpiAwarenessContext
        # The parameter is a handle-sized pseudo-handle: without this, ctypes passes -4 as a
        # 32-bit int and 64-bit Python can reject or truncate it.
        function.argtypes = [ctypes.c_void_p]
        function.restype = wintypes.BOOL
        if function(ctypes.c_void_p(DPI_AWARENESS_PER_MONITOR_V2)):
            return True
        _log("DPI awareness not set (already set, or refused); window sizes may be scaled")
    except Exception as exc:
        _log(f"DPI awareness not set: {exc}")
    return False


def is_minimized(hwnd, api=None):
    """True when ``hwnd`` is minimized (``IsIconic``); such a window is not restored."""
    api = api or _default_api()
    return bool(api and api.user32.IsIconic(hwnd))


def bgra_to_rgba(raw):
    """BGRA bytes (a GDI DIB) as RGBA with alpha forced to 255.

    ``PrintWindow`` leaves the alpha channel undefined (often 0), which would make a PNG
    transparent.
    """
    out = bytearray(raw)
    out[0::4] = raw[2::4]
    out[2::4] = raw[0::4]
    out[3::4] = b"\xff" * (len(raw) // 4)
    return bytes(out)


def capture_window(hwnd, api=None):
    """``(width, height, rgba)`` of window ``hwnd``, painted by ``PrintWindow``.

    The size is the window rectangle (``GetWindowRect``, physical pixels once the process is
    DPI aware). Every GDI object created here is released on every path. ``api`` (an object
    with ``user32`` and ``gdi32``) is for tests. Raises ``ValueError`` for an empty or absurd
    rectangle and ``OSError``/``RuntimeError`` when Windows refuses a step.
    """
    api = api or _default_api()
    if api is None:
        raise RuntimeError("window capture needs Windows")
    user32, gdi32 = api.user32, api.gdi32
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.pointer(rect)):
        raise OSError(f"GetWindowRect failed for window {hwnd}: the window is gone?")
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0:
        raise ValueError(f"window {hwnd} has no area ({width}x{height})")
    if width * height > MAX_PIXELS:
        raise ValueError(f"window {hwnd} is too large to capture ({width}x{height})")

    screen = user32.GetDC(None)
    if not screen:
        raise OSError("GetDC failed")
    memory = dib = previous = None
    try:
        memory = gdi32.CreateCompatibleDC(screen)
        if not memory:
            raise OSError("CreateCompatibleDC failed")
        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height  # negative: top-down rows, the order PNG wants
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = BI_RGB
        bits = ctypes.c_void_p()
        dib = gdi32.CreateDIBSection(memory, ctypes.pointer(info), DIB_RGB_COLORS,
                                     ctypes.pointer(bits), None, 0)
        if not dib or not bits.value:
            raise OSError("CreateDIBSection failed")
        previous = gdi32.SelectObject(memory, dib)
        if not previous:
            raise OSError("SelectObject failed")
        if not user32.PrintWindow(hwnd, memory, PW_RENDERFULLCONTENT):
            raise RuntimeError(f"PrintWindow failed for window {hwnd} "
                               "(the window may be gone or not paintable)")
        raw = ctypes.string_at(bits.value, width * height * BYTES_PER_PIXEL)
    finally:
        # Each release runs even if the one before it failed; none may hide the real error.
        for label, release in (
                ("SelectObject", lambda: previous and gdi32.SelectObject(memory, previous)),
                ("DeleteObject", lambda: dib and gdi32.DeleteObject(dib)),
                ("DeleteDC", lambda: memory and gdi32.DeleteDC(memory)),
                ("ReleaseDC", lambda: user32.ReleaseDC(None, screen))):
            try:
                release()
            except Exception as exc:
                _log(f"capture: {label} failed while releasing: {exc}")
    return width, height, bgra_to_rgba(raw)
