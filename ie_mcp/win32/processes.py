"""Process inspection through ctypes (no pywin32).

``process_identity`` is the pair (image name, creation time) that tells a process from a
different one that reused its PID. ``snapshot`` and ``process_tree`` list processes and their
descendants; the tree is for listing only, never for choosing what to kill (every kill goes
through ``session.kill_verified``).
"""
import ctypes
import ntpath
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
_PATH_CAPACITY = 32768  # the longest path Windows allows
TH32CS_SNAPPROCESS = 0x2
SNAPSHOT_TTL = 2.0  # seconds a process snapshot is reused

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


@dataclass(frozen=True)
class ProcessRow:
    pid: int
    ppid: int
    exe: str


class _ProcessEntry32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),  # ULONG_PTR: pointer-sized
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


_toolhelp = None


def _toolhelp32():
    """kernel32's Toolhelp functions with explicit prototypes, or None off Windows."""
    global _toolhelp
    if _toolhelp is None and sys.platform == "win32":
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        for name in ("Process32FirstW", "Process32NextW"):
            fn = getattr(k, name)
            fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32)]
            fn.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL
        _toolhelp = k
    return _toolhelp


def _log(message):
    from ie_mcp.session import log  # lazy: session imports this module
    log(message)


def _read_snapshot():
    """Every running process as ``ProcessRow`` (empty off Windows or when the call fails)."""
    k = _toolhelp32()
    if k is None:
        return []
    handle = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    # INVALID_HANDLE_VALUE is -1; as a pointer-sized HANDLE it reads back as all ones.
    if not handle or handle == ctypes.c_void_p(-1).value:
        _log(f"CreateToolhelp32Snapshot failed (error {ctypes.get_last_error()})")
        return []
    rows = []
    try:
        entry = _ProcessEntry32()
        entry.dwSize = ctypes.sizeof(_ProcessEntry32)
        ok = k.Process32FirstW(handle, ctypes.byref(entry))
        while ok:
            rows.append(ProcessRow(entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile))
            ok = k.Process32NextW(handle, ctypes.byref(entry))
    finally:
        k.CloseHandle(handle)
    return rows


_cache = None  # (time.monotonic() when read, rows)
_cache_lock = threading.Lock()


def snapshot(fresh=False):
    """The process list, reused for ``SNAPSHOT_TTL`` seconds unless ``fresh`` is true.

    A caller that compares a "before" and an "after" listing must pass ``fresh=True``: the
    cache would otherwise hand the second call the first call's data.
    """
    global _cache
    with _cache_lock:
        now = time.monotonic()
        if not fresh and _cache is not None and now - _cache[0] < SNAPSHOT_TTL:
            return list(_cache[1])
        rows = _read_snapshot()
        if not rows:
            # A failed read must not be reused for 2 s: every caller would see "no processes"
            # (``session_window_pids`` would shrink to the root) although the next read may work.
            _cache = None
            _log("process snapshot is empty; not caching it")
            return []
        _cache = (now, rows)
        return list(rows)


def process_tree(root_pid, rows=None):
    """``root_pid`` and every descendant by parent PID, as a set. For listing only.

    Quirk: a parent PID may have been reused by an unrelated process, so the tree can
    include strangers; that is why nothing is ever killed from this set. A parent cycle
    (the same reuse, seen twice) terminates.
    """
    if not root_pid:
        return set()
    rows = snapshot() if rows is None else rows
    children = {}
    for row in rows:
        children.setdefault(row.ppid, []).append(row.pid)
    tree, pending = {root_pid}, [root_pid]
    while pending:
        for child in children.get(pending.pop(), ()):
            if child not in tree:
                tree.add(child)
                pending.append(child)
    return tree
