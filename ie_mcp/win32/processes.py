"""Process inspection through ctypes (no pywin32).

This task adds only ``process_identity``: the pair (image name, creation time) that tells a
process from a different one that reused its PID.
"""
import ctypes
import ntpath
import sys
from ctypes import wintypes

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
_PATH_CAPACITY = 32768  # the longest path Windows allows

_api = None


def _kernel32():
    """kernel32 with explicit prototypes, or None off Windows.

    Quirk: ctypes assumes ``int`` for every argument and result unless told otherwise. A
    HANDLE is pointer-sized, so on 64-bit Windows a default-prototyped OpenProcess would
    truncate it. Hence every argtypes/restype below.
    """
    global _api
    if _api is None and sys.platform == "win32":
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL
        k.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        k.QueryFullProcessImageNameW.restype = wintypes.BOOL
        k.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        k.GetProcessTimes.restype = wintypes.BOOL
        k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k.GetExitCodeProcess.restype = wintypes.BOOL
        _api = k
    return _api


def process_identity(pid):
    """(image file name in lower case, creation time as a FILETIME int), or None.

    None means the process is gone (or cannot be queried). A PID can be reused by an
    unrelated process; the creation time changes then, so comparing the pair is what makes
    a kill by PID safe. A process that has exited but is still held open by someone else
    counts as gone.
    """
    k = _kernel32()
    if k is None:
        return None
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    handle = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        code = wintypes.DWORD()
        if not k.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != STILL_ACTIVE:
            return None
        buffer = ctypes.create_unicode_buffer(_PATH_CAPACITY)
        size = wintypes.DWORD(_PATH_CAPACITY)
        if not k.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not k.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                 ctypes.byref(kernel), ctypes.byref(user)):
            return None
        filetime = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return ntpath.basename(buffer.value).lower(), filetime
    finally:
        k.CloseHandle(handle)
