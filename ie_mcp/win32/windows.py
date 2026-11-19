"""Window enumeration and classification through ctypes (no pywin32, no COM).

Nothing here sends a message to a window: it only reads classes, titles, owners and child
lists, so it is safe to call from any thread, and it is what the modal probe runs on.
"""
import ctypes
import sys
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable, Optional, Union

from ie_mcp.win32 import processes

IE_SERVER_CLASS = "Internet Explorer_Server"
DIALOG_FRAME_CLASS = "Internet Explorer_TridentDlgFrame"  # a showModalDialog window
HELPER_CLASS = "Alternate Modal Top Most"  # owns script dialogs and showModalDialog windows (D6)
SCRIPT_DIALOG_CLASS = "#32770"  # alert, confirm, prompt (spike Q8); also every system dialog
PROMPT_EDIT_CONTROL_ID = 8133  # D14: a #32770 with an Edit child of this ID is a prompt
OWNER_CHAIN_LIMIT = 16  # owner chains are one to three deep in practice; this only stops loops
MODAL_KINDS = ("html_dialog", "script_dialog", "system_dialog")
GW_OWNER = 4
GA_ROOT = 2

# Top-level hwnds of attached tabs (``ie_attach`` fills it). For an attached tab only windows
# whose owner chain reaches one of these count, never the rest of the user's Edge process tree.
# Readers copy it (``set(ATTACHED_TOPS)``) while another thread may attach or detach, so a
# writer must REBIND a new set (``windows.ATTACHED_TOPS = {...}``), never mutate this one.
ATTACHED_TOPS: set = set()
# The process that owns the attached tab's IE server window: the helper window that owns the
# tab's script dialogs lives in it (see ``_attached_helpers`` and ``win32.dialogs``). Rebound
# atomically, like ATTACHED_TOPS.
ATTACHED_PIDS: frozenset = frozenset()
EDGE_IMAGE = "msedge.exe"


@dataclass
class WindowInfo:
    hwnd: int
    pid: int
    cls: str
    title: str
    kind: str  # main | popup | html_dialog | script_dialog | system_dialog | other
    owner_hwnd: Optional[int]
    visible: bool
    url: str = ""  # only filled when the caller asks (with_url=True); never by the probe
    is_prompt: bool = False
    # Class and process of the window's immediate owner ("" / None when it has none or it is
    # not a top-level window). A script dialog is owned by the helper or dialog frame (D6/Q8).
    owner_cls: str = ""
    owner_pid: Optional[int] = None


@dataclass
class TabInfo:
    """One IE-mode tab of a user's Edge window (see ``ie_mode_tabs``)."""
    hwnd: int              # the Internet Explorer_Server window (D15: it survives navigation)
    top_hwnd: int          # the browser window that holds it
    ancestors: list        # hwnd's ancestors, nearest first, ending at top_hwnd
    pid: int               # the process that owns the Internet Explorer_Server window
    title: str
    url: str = ""


@dataclass
class RawChild:
    hwnd: int
    cls: str
    ctrl_id: int
    visible: bool


@dataclass
class RawWindow:
    """One top-level window as ``enum_fn`` reports it.

    ``children`` is a list of ``RawChild`` or a zero-argument function returning one: the
    default enumeration reads a window's descendants only when a decision needs them.
    """
    hwnd: int
    pid: int
    cls: str
    title: str
    owner: Optional[int]
    visible: bool
    children: Union[list, Callable[[], list]]


# --- ctypes layer ------------------------------------------------------------------
_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM) \
    if sys.platform == "win32" else None
_user32_api = None


def _user32():
    """user32 with explicit prototypes, or None off Windows.

    Quirk: ctypes defaults every argument and result to a 32-bit ``int``. HWNDs and LPARAMs
    are pointer-sized, so on 64-bit Python a default-prototyped call truncates them.
    """
    global _user32_api
    if _user32_api is None and sys.platform == "win32":
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
        u.EnumWindows.restype = wintypes.BOOL
        u.EnumChildWindows.argtypes = [wintypes.HWND, _WNDENUMPROC, wintypes.LPARAM]
        u.EnumChildWindows.restype = wintypes.BOOL
        u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetClassNameW.restype = ctypes.c_int
        u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetWindowTextW.restype = ctypes.c_int
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowThreadProcessId.restype = wintypes.DWORD
        u.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        u.GetWindow.restype = wintypes.HWND
        u.IsWindowVisible.argtypes = [wintypes.HWND]
        u.IsWindowVisible.restype = wintypes.BOOL
        u.GetParent.argtypes = [wintypes.HWND]
        u.GetParent.restype = wintypes.HWND
        u.GetDlgCtrlID.argtypes = [wintypes.HWND]
        u.GetDlgCtrlID.restype = ctypes.c_int
        u.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        u.GetAncestor.restype = wintypes.HWND
        _user32_api = u
    return _user32_api


def class_name(hwnd):
    """The window class of ``hwnd``, or ``""`` when the window is gone."""
    u = _user32()
    if u is None:
        return ""
    buffer = ctypes.create_unicode_buffer(256)  # class names are limited to 256 characters
    return buffer.value if u.GetClassNameW(hwnd, buffer, 256) else ""


def is_visible(hwnd):
    u = _user32()
    return bool(u and u.IsWindowVisible(hwnd))


def window_pid(hwnd):
    """The process id that owns ``hwnd``, or None when it is not a window (read-only)."""
    u = _user32()
    if u is None:
        return None
    pid = wintypes.DWORD()
    return int(pid.value) if u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)) else None


def root_window(hwnd):
    """The top-level window above ``hwnd`` (``GetAncestor(GA_ROOT)``; ``hwnd`` itself if it is one).

    0 when ``hwnd`` is not a window. A tab's ``Internet Explorer_Server`` window resolves to
    the browser window that holds it. Read-only: nothing is sent to any window.
    """
    u = _user32()
    return int(u.GetAncestor(hwnd, GA_ROOT) or 0) if u else 0


def ancestor_chain(hwnd):
    """``hwnd``'s parents, nearest first, ending at its top-level window ([] for a top window).

    Read-only. ``GetParent`` on a child is its parent window (on a top-level window it would be
    the owner, which is why the walk stops at the ``GA_ROOT`` window).
    """
    u = _user32()
    if u is None:
        return []
    top, chain, current = root_window(hwnd), [], hwnd
    while top and current != top and len(chain) < 64:
        current = int(u.GetParent(current) or 0)
        if not current:
            break
        chain.append(current)
    return chain


def _window_text(u, hwnd):
    # GetWindowText on another process's window reads the caption without sending it a
    # message, so a hung IE process cannot block this.
    buffer = ctypes.create_unicode_buffer(1024)
    return buffer.value if u.GetWindowTextW(hwnd, buffer, 1024) else ""


def _collect(enumerate_fn, parent=None):
    """Handles of the windows ``EnumWindows``/``EnumChildWindows`` report, in order."""
    found = []

    def callback(hwnd, _lparam):
        if hwnd:
            found.append(int(hwnd))
        return True  # keep enumerating; an exception here would be printed and swallowed

    proc = _WNDENUMPROC(callback)  # must stay referenced until the enumeration returns
    if parent is None:
        enumerate_fn(proc, 0)
    else:
        enumerate_fn(parent, proc, 0)
    return found


def enum_descendants(hwnd):
    """Every descendant of ``hwnd`` (hidden ones too), as ``RawChild`` rows."""
    u = _user32()
    if u is None:
        return []
    return [RawChild(h, class_name(h), int(u.GetDlgCtrlID(h)), bool(u.IsWindowVisible(h)))
            for h in _collect(u.EnumChildWindows, hwnd)]


def enum_children(hwnd):
    """The direct children of ``hwnd`` (its descendants whose parent is ``hwnd``), as ``RawChild``.

    Quirk (seen live): a script dialog shaped as a TaskDialog has one direct child, a
    ``DirectUIHWND``, but a dozen hidden descendants inside it (``CtrlNotifySink``,
    ``ScrollBar``, ``SysLink``, a ``Button``). Spike Q8's shapes are the direct children.
    """
    u = _user32()
    if u is None:
        return []
    return [c for c in enum_descendants(hwnd) if int(u.GetParent(c.hwnd) or 0) == hwnd]


def _enum_top_level():
    """Every top-level window as ``RawWindow``; descendants are read lazily."""
    u = _user32()
    if u is None:
        return []
    out = []
    for hwnd in _collect(u.EnumWindows):
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        owner = u.GetWindow(hwnd, GW_OWNER)
        out.append(RawWindow(
            hwnd=hwnd, pid=int(pid.value), cls=class_name(hwnd), title=_window_text(u, hwnd),
            owner=int(owner) if owner else None, visible=bool(u.IsWindowVisible(hwnd)),
            children=lambda h=hwnd: enum_descendants(h)))
    return out


# --- classification ------------------------------------------------------------------
def classify(cls, has_ie_server_child, owner_in_session, hwnd, main_hwnd, is_prompt=False):
    """The kind of a window; see ``WindowInfo.kind``.

    A ``#32770`` that a session window owns is only a *candidate* script dialog: a system
    dialog the page caused (file open/save, print, a security prompt) is owned by the same
    window and classifies the same way through ``owner_in_session``. Task 9 must confirm the
    Q8 shape (alert: Button 2 + Static 20; confirm: only a DirectUIHWND; prompt: an Edit 8133)
    before it acts, because system dialogs must never be clicked. Any other ``#32770`` is a
    ``system_dialog``.
    """
    if is_prompt and owner_in_session:
        return "script_dialog"
    if cls == DIALOG_FRAME_CLASS:
        return "html_dialog"
    if cls == SCRIPT_DIALOG_CLASS:
        return "script_dialog" if owner_in_session else "system_dialog"
    if has_ie_server_child:
        return "main" if hwnd == main_hwnd else "popup"
    return "other"


def _session():
    # Imported lazily: session imports this package for main_hwnd, and tests replace SESSION.
    from ie_mcp import session
    return session.SESSION


def session_window_pids():
    """PIDs of the session's IE-mode Edge process tree (empty without a session).

    D6: content, script dialogs and showModalDialog windows all live in the Edge tree. The
    tree is built from parent PIDs, which is fine for listing; nothing is killed from it.
    """
    edge_pid = getattr(_session(), "edge_pid", None)
    return processes.process_tree(edge_pid) if edge_pid else set()


def session_windows(with_url=False):
    """Every window of the session as ``WindowInfo`` (``with_url`` needs a COM apartment)."""
    return enumerate_windows(session_window_pids(), with_url=with_url,
                             attached_tops=set(ATTACHED_TOPS))


def other_session_pids():
    """PIDs of the process trees of the OTHER live ie-mcp sessions (from their owner records).

    ``ie_mode_tabs`` must never offer their windows: a second agent's browser is not a tab
    the user opened. Dead sessions are not excluded (nothing runs there).
    """
    from ie_mcp import config, session  # lazy: session imports this package
    paths = config.SETTINGS.session_paths
    roots = session.live_edge_roots(paths.root, paths.session_id)
    rows = processes.snapshot()
    pids = set()
    for root in roots:
        pids |= processes.process_tree(root, rows)
    return pids


def other_sessions_starting():
    """How many other live ie-mcp sessions are starting a browser (see ``session.starting_sessions``)."""
    from ie_mcp import config, session  # lazy: session imports this package
    paths = config.SETTINGS.session_paths
    return session.starting_sessions(paths.root, paths.session_id)


def ie_mode_tabs(enum_fn=None):
    """The IE-mode tabs of user-visible Edge windows, as ``TabInfo`` (one per visible IE server).

    A top window counts when it is visible, belongs to an ``msedge.exe`` process and has a
    visible ``Internet Explorer_Server`` descendant. Windows in the process trees of other live
    ie-mcp sessions are left out. The URL is read through COM (the caller must be in a COM
    apartment; ``""`` when pywin32 or the read is missing). Inactive tabs have a hidden server
    window and are not listed: activate the tab in the browser first.
    """
    rows = processes.snapshot()
    edge = {r.pid for r in rows if r.exe.lower() == EDGE_IMAGE}
    if not edge:
        return []
    other = other_session_pids()
    tabs = []
    for w in (enum_fn or _enum_top_level)():
        if not w.visible or w.pid not in edge or w.pid in other:
            continue
        for server in (c for c in _children(w) if c.cls == IE_SERVER_CLASS and c.visible):
            pid = window_pid(server.hwnd) or w.pid
            if pid in other:
                continue
            tabs.append(TabInfo(hwnd=server.hwnd, top_hwnd=w.hwnd,
                                ancestors=ancestor_chain(server.hwnd), pid=pid, title=w.title,
                                url=_server_url(server.hwnd)))
    return tabs


def _server_url(hwnd):
    try:
        from ie_mcp.win32 import htmldoc  # lazy: keeps pywin32 off the probe path
        return str(htmldoc.document_url(hwnd) or "")
    except Exception as exc:
        _log_url_failure(hwnd, exc)
        return ""


def find_window(hwnd, with_url=False):
    """The session's ``WindowInfo`` for ``hwnd``, or None when it is not one of its windows."""
    return next((w for w in session_windows(with_url) if w.hwnd == hwnd), None)


def pick_main_hwnd(before, after, driver_title):
    """The IE top window a new session opened.

    ``before``: hwnds that had an IE server before the driver started (None when that
    snapshot failed). ``after``: {hwnd: title} afterwards. One new window is the answer; with
    several, the one whose title starts with the driver's page title (``driver_title`` is a
    function, called only then); otherwise None.
    """
    if before is None:
        return None
    new = {h: t for h, t in after.items() if h not in before}
    if len(new) == 1:
        return next(iter(new))
    if len(new) > 1:
        try:
            title = driver_title()
        except Exception:
            return None
        if title:
            for hwnd, text in new.items():
                if text.startswith(title):
                    return hwnd
    return None


def ie_top_windows(pids=None, enum_fn=None):
    """{hwnd: title} of the top windows that have an ``Internet Explorer_Server`` descendant.

    ``pids`` limits the scan to those processes (None: every process).
    """
    out = {}
    for w in (enum_fn or _enum_top_level)():
        if pids is not None and w.pid not in pids:
            continue
        if any(c.cls == IE_SERVER_CLASS for c in _children(w)):
            out[w.hwnd] = w.title
    return out


def _children(window):
    """The window's descendants, read once (``children`` may be a lazy function)."""
    if callable(window.children):
        window.children = window.children()
    return window.children


def _is_prompt(window):
    return window.cls == SCRIPT_DIALOG_CLASS and any(
        c.cls == "Edit" and c.ctrl_id == PROMPT_EDIT_CONTROL_ID for c in _children(window))


def _has_ie_server(window):
    return any(c.cls == IE_SERVER_CLASS for c in _children(window))


def enumerate_windows(pids, enum_fn=None, with_url=False, attached_tops=None):
    """Classify the top-level windows of ``pids`` (and of attached tabs) as ``WindowInfo``.

    A window is listed when its process is in ``pids`` or when its owner chain reaches one of
    ``attached_tops``. ``owner_in_session`` is true when an owner in that chain is a session
    window (a main/popup window or ``html_dialog`` of ``pids``, the ``Alternate Modal Top
    Most`` helper of ``pids`` (D6), or an attached top). ``with_url`` reads each document's
    URL through COM, so only a caller that already entered a COM apartment (a tool worker)
    may ask for it; a failed read leaves ``""``.
    """
    pids = set(pids or ())
    attached = set(attached_tops or ())
    raw = list((enum_fn or _enum_top_level)())
    by_hwnd = {w.hwnd: w for w in raw}
    attached |= _attached_helpers(raw, attached)
    main_hwnd = getattr(_session(), "main_hwnd", None)

    def owner_chain(window):
        seen, current = {window.hwnd}, window.owner
        while current and current not in seen and len(seen) <= OWNER_CHAIN_LIMIT:
            seen.add(current)
            yield current
            owner = by_hwnd.get(current)
            current = owner.owner if owner else None

    def is_session_window(window):
        if window.hwnd in attached:
            return True
        if window.pid not in pids:
            return False
        return window.cls in (HELPER_CLASS, DIALOG_FRAME_CLASS) or _has_ie_server(window)

    out = []
    for w in raw:
        chain = list(owner_chain(w)) if (w.pid in pids or attached) else []
        reaches_attached = any(h in attached for h in chain)
        if w.pid not in pids and not reaches_attached:
            continue
        owner_in_session = reaches_attached or any(
            h in by_hwnd and is_session_window(by_hwnd[h]) for h in chain)
        prompt = _is_prompt(w)
        kind = classify(w.cls, _has_ie_server(w) if w.cls != SCRIPT_DIALOG_CLASS else False,
                        owner_in_session, w.hwnd, main_hwnd, is_prompt=prompt)
        owner = by_hwnd.get(w.owner) if w.owner else None
        info = WindowInfo(hwnd=w.hwnd, pid=w.pid, cls=w.cls, title=w.title, kind=kind,
                          owner_hwnd=w.owner, visible=w.visible, url="", is_prompt=prompt,
                          owner_cls=owner.cls if owner else "",
                          owner_pid=owner.pid if owner else None)
        if with_url and kind in ("main", "popup", "html_dialog"):
            info.url = _read_url(w)
        out.append(info)
    return out


def _attached_helpers(raw, attached):
    """The ``Alternate Modal Top Most`` helpers that belong to an attached tab.

    Quirk (seen live): in the user's Edge the helper that owns a page's script dialogs is an
    ownerless top-level window of the tab's IE process (``ATTACHED_PIDS``), not owned by the
    browser window, so the owner chain of an alert never reaches the attached top. The helper is
    taken as the tab's only when its caption (the page title) is not empty and exactly one
    visible window carries it, and that one is an attached top: with two same-titled windows
    the dialog could be the other one's, and a confirm of another window must never be
    answered as this tab's.
    """
    if not attached or not ATTACHED_PIDS:
        return set()
    carriers = {}
    for w in raw:
        if w.visible and w.title and w.cls not in (HELPER_CLASS, SCRIPT_DIALOG_CLASS,
                                                  DIALOG_FRAME_CLASS):
            carriers.setdefault(w.title, []).append(w.hwnd)
    sole = {title for title, hwnds in carriers.items() if len(hwnds) == 1 and hwnds[0] in attached}
    return {w.hwnd for w in raw if w.cls == HELPER_CLASS and w.pid in ATTACHED_PIDS
            and w.title in sole}


def _read_url(window):
    server = next((c for c in _children(window) if c.cls == IE_SERVER_CLASS and c.visible),
                  next((c for c in _children(window) if c.cls == IE_SERVER_CLASS), None))
    return "" if server is None else _server_url(server.hwnd)


_warned = set()


def _log_url_failure(hwnd, exc):
    """Say why a URL is empty, so ``ie_windows`` showing blanks is not a mystery.

    A missing pywin32 is the same for every window, so it is logged once.
    """
    if isinstance(exc, RuntimeError) and "pywin32" in str(exc):
        if "pywin32" in _warned:
            return
        _warned.add("pywin32")
    from ie_mcp.session import log  # lazy: session imports this package
    log(f"could not read the URL of window {hwnd}: {exc}")


def modal_probe():
    """The windows that block the page, as dicts (installed as ``protocol.MODAL_PROBE``).

    ``html_dialog``, ``script_dialog`` and ``system_dialog`` windows of the session's
    processes, plus those owned (directly or through the owner chain) by an attached tab.
    Win32 only: no COM, no pywin32, never a ``url``. Only *visible* windows are reported: an
    Edge process owns hidden ``#32770`` windows, which would put a false NOTICE on every tool
    result.
    """
    pids, attached = session_window_pids(), set(ATTACHED_TOPS)
    if not pids and not attached:
        return []
    return [
        {"hwnd": w.hwnd, "kind": w.kind, "title": w.title, "pid": w.pid, "cls": w.cls,
         "owner_hwnd": w.owner_hwnd, "is_prompt": w.is_prompt}
        for w in enumerate_windows(pids, attached_tops=attached)
        if w.visible and w.kind in MODAL_KINDS]
