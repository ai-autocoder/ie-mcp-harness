"""The two ways a tool reaches a page: the Selenium driver, or COM straight into the document.

``resolve_backend(target)`` returns a ``DriverBackend`` for the session's driver (the default)
or a ``ComBackend`` for a window the driver cannot serve: a ``showModalDialog`` window, or a
tab the user opened. Both implement the same interface, so each tool is written once.

COM lifetime: a ``ComBackend`` is created per tool call (or per hook thread), caches its
document on the instance and refuses to be used from any other thread, so no COM object
outlives the thread that obtained it. Nothing here keeps a COM object in a module global.

All DOM access over COM is script (spike D7): in a documentMode 11 document win32com's dynamic
dispatch cannot see DOM methods or expandos, but ``execScript`` runs, and an expando is read
back with ``IDispatchEx``. An element "handle" is therefore only its locator (``ComElement``):
every operation finds the element again inside its own script, so nothing is registered in the
page and a node that was replaced since ``find`` is found again.
"""
import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Optional, Protocol

from ie_mcp import js
from ie_mcp.session import SESSION, log
from ie_mcp.win32 import htmldoc, messages, windows

IE_SERVER_CLASS = htmldoc.IE_SERVER_CLASS

# "driver", "hwnd:<n>" or "tab:<n>" to use when a call names no target. Task 15 sets it (after
# ie_attach); it lives here, not in protocol, so nothing imports protocol. Read at call time.
DEFAULT_TARGET: Optional[str] = None

# For each attached tab's Internet Explorer_Server hwnd: its ancestor hwnds, nearest first, up to
# the top window. Task 15 fills it.
ATTACHED_CHAINS: dict = {}

# Class of the ancestor window that owns a tab's one IE server window, for finding that server
# again when its own hwnd is gone. D15 = ie-server: the IE server hwnd survived every navigation
# the spike made, and it named no ancestor class, so there is none and a tab whose server window
# is gone must be attached again (never "the first IE server under the top window": that could
# be another of the user's tabs).
TAB_ANCESTOR_CLASS: Optional[str] = None

TARGET_HELP = (
    "Where to act: omit it for the session's IE driver; hwnd:<n> acts over COM on the window "
    "<n> (a modal dialog's hwnd from a NOTICE or ie_windows), tab:<n> on an attached tab. "
    "Over COM, DOM access is script, input is synthetic (script events and a timer click, not "
    "mouse or keyboard input; keys are posted window messages) and only id, name, tag, class, "
    "link_text and (documentMode 8+) css locators work.")
TARGET_SCHEMA = {"type": "string", "description": TARGET_HELP}

COM_LOCATORS = ("id", "name", "tag", "class", "link_text", "css")
_TARGET = re.compile(r"(hwnd|tab):([0-9]+)")


class OptionNotFound(ValueError):
    """The requested <option> is not in the <select>."""


class WindowClosed(RuntimeError):
    """The window went away while a COM request ran (the page's own script closed it)."""


class NoSuchElement(LookupError):
    """No element matched the locator within the timeout (COM backend)."""


# --- targets --------------------------------------------------------------------------------
def _parse_target(target):
    """``(kind, hwnd)`` for a target string after applying ``DEFAULT_TARGET``; never touches COM."""
    if target is None or target == "":
        target = DEFAULT_TARGET
    if target is None or target == "driver":
        return "driver", None
    if isinstance(target, str):
        match = _TARGET.fullmatch(target)
        if match and int(match.group(2)) > 0:
            return match.group(1), int(match.group(2))
    raise ValueError(f"unknown target {target!r}: use driver, hwnd:<n> or tab:<n>")


def default_tab_hwnd():
    """The number of the attached tab that ``DEFAULT_TARGET`` names, or None (no tab default)."""
    kind, number = _parse_target(None)
    return number if kind == "tab" else None


# --- the attached tab (state owner; ``ie_attach`` / ``ie_detach`` call these) ---------------
# Held for every change of the attachment and by readers that check-then-act on it. The state
# itself (DEFAULT_TARGET, ATTACHED_CHAINS, windows.ATTACHED_TOPS/PIDS) is rebound, never mutated,
# because other threads read it without the lock.
ATTACH_LOCK = threading.RLock()


def attached_tab():
    """The hwnd of the attached tab (``DEFAULT_TARGET`` is ``tab:<n>``), or None. Never raises."""
    try:
        return default_tab_hwnd()
    except ValueError:
        return None


def attach_tab(tab):
    """Make ``tab`` (a ``windows.TabInfo``) the attached tab, replacing any other in one swap.

    ``DEFAULT_TARGET`` is set last and is never None in between: a concurrent driver tool is
    refused throughout a re-attach.
    """
    global DEFAULT_TARGET, ATTACHED_CHAINS
    with ATTACH_LOCK:
        windows.ATTACHED_PIDS = frozenset({tab.pid})
        windows.ATTACHED_TOPS = {tab.top_hwnd}
        ATTACHED_CHAINS = {**ATTACHED_CHAINS, tab.hwnd: list(tab.ancestors)}
        DEFAULT_TARGET = f"tab:{tab.hwnd}"
        ATTACHED_CHAINS = {tab.hwnd: list(tab.ancestors)}


def detach_tab():
    """Clear the attachment (``DEFAULT_TARGET`` first); the number of the tab that was attached."""
    global DEFAULT_TARGET, ATTACHED_CHAINS
    with ATTACH_LOCK:
        number = attached_tab()
        DEFAULT_TARGET = None
        windows.ATTACHED_TOPS = set()
        windows.ATTACHED_PIDS = frozenset()
        ATTACHED_CHAINS = {}
        return number


def prune_attached():
    """Detach when the attached tab or its top window is gone; the tab number dropped, else None.

    A dead top must not linger in ``ATTACHED_TOPS``: its handle could be reused by an unrelated
    window, which would then count as part of the session.
    """
    with ATTACH_LOCK:
        number = attached_tab()
        if number is None:
            return None
        if messages.is_window(number) and all(messages.is_window(h) for h in windows.ATTACHED_TOPS):
            return None
        return detach_tab()


def target_kind(target):
    """``"driver"``, ``"hwnd"`` or ``"tab"``; ValueError for any other form."""
    return _parse_target(target)[0]


def uses_driver(args):
    """``needs_driver`` of the target-aware tools: only the driver target needs the driver lock."""
    return target_kind(args.get("target")) == "driver"


def check_target(kind, hwnd):
    """Refuse (ValueError) a ``hwnd:``/``tab:`` target that is not a window of this session.

    Win32 only, before any COM call (the AFTER_CALL drain resolves through here too, so a
    refused window is never instrumented). Accepted: a window whose top-level window is one of
    the session's (``windows.find_window``) or an attached tab's top; the attached tab; and,
    for ``tab:`` only, a tab ``windows.ie_mode_tabs`` lists (which leaves out other live
    sessions). A stale hwnd that Windows gave to another window fails the same checks.
    """
    if kind == "tab" and hwnd == attached_tab():
        return
    root = windows.root_window(hwnd) or hwnd
    if root in windows.ATTACHED_TOPS or windows.find_window(root) is not None:
        return
    if kind == "tab":
        if any(t.hwnd == hwnd for t in windows.ie_mode_tabs(with_url=False)):
            # The drain instruments this unattached tab of the user's like an attached one, so
            # the Fidelity row "instrumentation left in an attached tab" must show.
            from ie_mcp.tools import attach  # lazy: the tools import this module
            attach.USED = True
            return
        raise ValueError(f"tab:{hwnd} is not a window of this session (list the user's tabs "
                         "with ie_tabs, then ie_attach one)")
    raise ValueError(f"hwnd:{hwnd} is not a window of this session (see ie_windows)")


def resolve_backend(target):
    """The backend for ``target`` (None: ``DEFAULT_TARGET``, else the driver).

    A ``hwnd:``/``tab:`` target must pass ``check_target`` first (ValueError otherwise).
    """
    kind, hwnd = _parse_target(target)
    if kind == "driver":
        return DriverBackend()
    check_target(kind, hwnd)
    return ComBackend(hwnd, kind=kind)


# --- shared parsing -------------------------------------------------------------------------
def parse_frames(raw):
    """The records frames.js returns (index, name, id, src) as a list of dicts."""
    out = []
    for record in (raw or "").split("\x1e"):
        if record == "":
            continue
        fields = record.split("\x1f") + [""] * 4
        out.append({"index": int(fields[0]), "name": fields[1], "id": fields[2],
                    "src": fields[3]})
    return out


def build_grid(raw, headers, limit):
    """Turn grid.js's delimited string into the ie_grid result (header-keyed rows when possible)."""
    if not raw:
        return {"rows": 0, "note": "no table found"}
    grid = [r.split("\x1f") for r in str(raw).split("\x1e") if r != ""]
    grid = [r for r in grid if any(c.strip() for c in r)]  # drop spacer/empty rows
    if not headers and grid:
        # detect header row: first row whose cells are all non-empty & non-numeric-ish
        first = grid[0]
        if first and all(c.strip() and not c.strip().isdigit() for c in first):
            headers = first
            grid = grid[1:]
    grid = grid[:limit]
    if headers and all(len(r) == len(headers) for r in grid):
        rows = [dict(zip(headers, r)) for r in grid]
    else:
        rows = grid  # fall back to arrays
    return {"columns": len(headers) if headers else (len(grid[0]) if grid else 0),
            "headers": headers, "row_count": len(rows), "rows": rows}


class Backend(Protocol):
    """What a tool needs from a page, whichever way it is reached."""

    def ensure(self) -> None: ...
    def refresh(self) -> None: ...
    def reset_frame(self) -> None: ...
    def html(self, frame) -> str: ...
    def text(self, frame) -> str: ...
    def js(self, script, frame, args) -> object: ...
    def find(self, by, value, frame, timeout) -> object: ...
    def click(self, el, action, mode) -> None: ...
    def queues_click(self, action, mode) -> bool: ...
    def fill(self, el, text, clear) -> None: ...
    def select(self, el, how, val) -> str: ...
    def get(self, el, attr) -> str: ...
    def press_key(self, el, key, frame) -> None: ...
    def hover(self, el) -> None: ...
    def scroll(self, frame, el, to, dx, dy) -> str: ...
    def grid(self, frame, table_id, headers, limit) -> dict: ...
    def frames(self) -> list: ...


# --- the driver ---------------------------------------------------------------------------
# Queue the click on the page's own timer without waiting for its handlers. A WebDriver click
# whose handler opens alert/confirm/showModalDialog can block until that dialog closes; a
# timer callback cannot block this call. The price: the click is script-initiated.
ASYNC_CLICK_SCRIPT = ("var e=arguments[0];window.setTimeout(function(){e.click();},0);"
                      "return 'queued';")

_KEY_MAP = None


def _driver_key(name):
    """Selenium's key for a name like "Enter"; anything else is sent as typed."""
    global _KEY_MAP
    from selenium.webdriver.common.keys import Keys
    if _KEY_MAP is None:
        _KEY_MAP = {
            "enter": Keys.ENTER, "return": Keys.RETURN, "tab": Keys.TAB,
            "escape": Keys.ESCAPE, "esc": Keys.ESCAPE, "space": Keys.SPACE,
            "backspace": Keys.BACK_SPACE, "delete": Keys.DELETE, "del": Keys.DELETE,
            "up": Keys.ARROW_UP, "down": Keys.ARROW_DOWN, "left": Keys.ARROW_LEFT,
            "right": Keys.ARROW_RIGHT, "arrowup": Keys.ARROW_UP, "arrowdown": Keys.ARROW_DOWN,
            "arrowleft": Keys.ARROW_LEFT, "arrowright": Keys.ARROW_RIGHT,
            "home": Keys.HOME, "end": Keys.END, "pageup": Keys.PAGE_UP,
            "pagedown": Keys.PAGE_DOWN, "f5": Keys.F5, "insert": Keys.INSERT,
        }
    return _KEY_MAP.get(name.lower(), name)


class DriverBackend:
    """The session's Selenium driver. Stateless: every call goes through ``SESSION``."""

    kind = "driver"

    def ensure(self):
        SESSION.ensure()

    def refresh(self):
        """Nothing to drop: the driver re-reads the page itself."""

    def reset_frame(self):
        try:
            SESSION.driver.switch_to.default_content()
        except Exception:
            pass

    def html(self, frame):
        d = SESSION.ensure()
        SESSION._apply_frame(frame)
        return d.page_source

    def text(self, frame):
        from selenium.webdriver.common.by import By
        d = SESSION.ensure()
        SESSION._apply_frame(frame)
        try:
            txt = d.find_element(By.TAG_NAME, "body").text
        except Exception:
            # Selenium's text atom fails on Trident; plain innerText still works.
            txt = d.execute_script("return document.body.innerText")
        return "" if txt is None else str(txt)

    def js(self, script, frame, args):
        d = SESSION.ensure()
        SESSION._apply_frame(frame)
        return d.execute_script(script, *args)

    def find(self, by, value, frame, timeout):
        from selenium.common.exceptions import NoSuchElementException
        try:
            return SESSION.find(by, value, frame, timeout)
        except NoSuchElementException as exc:
            # One "not found" class for the tools, whichever backend they use.
            raise NoSuchElement(str(exc).strip() or "no such element") from exc

    def queues_click(self, action, mode):
        return action == "click" and mode == "async"

    def click(self, el, action, mode):
        if action == "click" and mode == "async":
            SESSION.ensure().execute_script(ASYNC_CLICK_SCRIPT, el)
        elif action == "click":
            el.click()
        else:
            from selenium.webdriver.common.action_chains import ActionChains
            chain = ActionChains(SESSION.driver)
            if action == "double":
                chain.double_click(el).perform()
            else:
                chain.context_click(el).perform()

    def fill(self, el, text, clear):
        if clear:
            el.clear()
        el.send_keys(text)

    def select(self, el, how, val):
        # Selenium's Select clicks the <option>, which the legacy Trident engine rejects with
        # "Error executing JavaScript", so the option is set directly (select.js).
        res = SESSION.ensure().execute_script(js.load("select.js"), el, how, val)
        if res == "NOTFOUND":
            raise OptionNotFound(f"option {how}={val!r} not found")
        return res[3:]

    def get(self, el, attr):
        # el.text / el.get_attribute use IEDriver's JS atoms, which fail on the legacy engine;
        # arguments[0].innerText/value/getAttribute is simple enough to work.
        d = SESSION.ensure()
        try:
            if attr == "text":
                out = d.execute_script("return arguments[0].innerText;", el)
            elif attr == "value":
                out = d.execute_script("return arguments[0].value;", el)
            else:
                out = d.execute_script("return arguments[0].getAttribute(arguments[1]);", el, attr)
        except Exception:
            out = el.text if attr == "text" else el.get_attribute(attr)
        return "" if out is None else str(out)

    def press_key(self, el, key, frame):
        d = SESSION.ensure()
        key = _driver_key(key)
        if el is not None:
            el.send_keys(key)
        else:
            SESSION._apply_frame(frame)
            d.switch_to.active_element.send_keys(key)

    def hover(self, el):
        from selenium.webdriver.common.action_chains import ActionChains
        ActionChains(SESSION.ensure()).move_to_element(el).perform()

    def scroll(self, frame, el, to, dx, dy):
        d = SESSION.ensure()
        if el is not None and not to:
            d.execute_script("arguments[0].scrollIntoView(true);", el)
            return "scrolled into view"
        if to in ("top", "bottom"):
            if el is not None:
                pos = "0" if to == "top" else "arguments[0].scrollHeight"
                d.execute_script(f"arguments[0].scrollTop = {pos};", el)
                return f"scrolled container to {to}"
            SESSION._apply_frame(frame)
            y = "0" if to == "top" else "document.body.scrollHeight"
            d.execute_script(f"window.scrollTo(0, {y});")
            return f"scrolled page to {to}"
        SESSION._apply_frame(frame)
        d.execute_script("window.scrollBy(arguments[0], arguments[1]);", dx, dy)
        return f"scrolled by ({dx},{dy})"

    def grid(self, frame, table_id, headers, limit):
        d = SESSION.ensure()
        SESSION._apply_frame(frame)
        return build_grid(d.execute_script(js.load("grid.js"), table_id), headers, limit)

    def frames(self):
        d = SESSION.ensure()
        d.switch_to.default_content()
        # frames.js reads name/id/src from the frame elements of the top document (Selenium's
        # get_attribute atom fails on Trident) and returns a delimited string.
        return parse_frames(d.execute_script(js.load("frames.js")))


# --- COM --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ComElement:
    """A COM element handle: its locator. Each operation finds the element again in its own script."""

    by: str
    value: str
    frame: object = None


# Run after every request: the only state left in the page is these two empty expandos.
CLEAR_SCRIPT = "window.__iemcp_r = null; window.__iemcp_e = '';"
# Evaluated to read the document mode of the target (the key path depends on it).
DOC_MODE_BODY = "return document.documentMode || 0;"
_FIND_BODY = "return iemcpFind(arguments[0], arguments[1]) ? 1 : 0;"
_ELEMENT = "var el = iemcpElement(arguments[0], arguments[1]);\n"
_TEXT_BODY = "return document.body ? document.body.innerText : '';"
_HTML_BODY = "return document.documentElement ? document.documentElement.outerHTML : '';"
_FILL_BODY = _ELEMENT + (
    "try{ el.focus(); }catch(x){}\n"
    "el.value = arguments[3] ? arguments[2] : el.value + arguments[2];\n"
    "var evs = ['onkeydown','onkeypress','onkeyup','onchange','onblur'], i, bad = [];\n"
    "for(i = 0; i < evs.length; i++){ if(!iemcpFire(el, evs[i])){ bad.push(evs[i]); } }\n"
    "return bad.length ? 'EVENTS:' + bad.join(',') : 'ok';")
_HOVER_BODY = _ELEMENT + ("var bad = [];\n"
                          "if(!iemcpFire(el, 'onmouseover')){ bad.push('onmouseover'); }\n"
                          "if(!iemcpFire(el, 'onmouseenter')){ bad.push('onmouseenter'); }\n"
                          "return bad.length ? 'EVENTS:' + bad.join(',') : 'ok';")
# Queued on a timer, like the driver's async click, for the same reason: a click that opens a
# modal dialog must not block this call (it would hang the COM call that runs it). For the same
# reason it cannot report whether the click worked: it has not run yet when this call returns.
_CLICK_BODY = _ELEMENT + ("window.setTimeout(function(){ try{ el.click(); }catch(x){} }, 0);\n"
                          "return 'ok';")
_EVENT_CLICK_BODY = _ELEMENT + "iemcpFire(el, '%s');\nreturn 'ok';"
_GET_BODIES = {
    "text": _ELEMENT + "var t = el.innerText;\nif(t === undefined){ t = el.textContent; }\nreturn t;",
    "value": _ELEMENT + "return el.value;",
}
_GET_ATTR_BODY = _ELEMENT + "return el.getAttribute(arguments[2]);"
_FOCUS_BODY = _ELEMENT + "try{ el.focus(); }catch(x){}\nreturn 'ok';"
_FOCUS_WINDOW_BODY = "try{ window.focus(); }catch(x){}\nreturn 'ok';"
# Old document modes (below 7) have no posted-key path (D7 measured 7 and 11): fire the key
# events with a keyCode instead. It is synthetic: no character reaches a text field.
_FIRE_KEY_BODY = (
    "var el = arguments[0] ? iemcpElement(arguments[0], arguments[1])"
    " : (document.activeElement || document.body);\n"
    "var names = ['onkeydown','onkeypress','onkeyup'], i, ev;\n"
    "for(i = 0; i < names.length; i++){\n"
    "  try{ ev = document.createEventObject(); ev.keyCode = arguments[2]; "
    "el.fireEvent(names[i], ev); }catch(x){}\n"
    "}\n"
    "return 'ok';")
_SELECT_BODY = _ELEMENT + "return (function(){\n%s\n})(el, arguments[2], arguments[3]);"

_STRIP_COMMENTS = re.compile(r"^[ \t]*//[^\n]*\n", re.M)
_PLACEHOLDER = re.compile(r"@(PATH|RUN|TARGET|FUNC|ARGS)@")


def js_literal(value):
    """``value`` as an ES3 literal that is safe inside an HTML script and in any document mode.

    Only str, int, float, bool and None are allowed. Strings are plain ASCII: every non-ASCII
    character, quote, backslash, newline, U+2028/2029 is an escape, and ``</`` becomes ``<\\/``,
    so the text can never close a script element or end the literal early.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("NaN and infinity cannot be passed as script arguments")
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True).replace("</", "<\\/")
    raise ValueError(f"script arguments must be str, int, float, bool or None, not "
                     f"{type(value).__name__}")


def frame_path(frame):
    """The frame argument as a list of segments: an index (int) or a name (str) each.

    The same forms the driver tools accept: one index or name, "a/b" or a list for nesting.
    """
    if frame is None or frame == "":
        return []
    if isinstance(frame, (list, tuple)):
        segments = list(frame)
    elif isinstance(frame, str) and "/" in frame:
        segments = [s for s in frame.split("/") if s != ""]
    else:
        segments = [frame]
    out = []
    for seg in segments:
        if isinstance(seg, bool) or not isinstance(seg, (int, str)):
            raise ValueError(f"bad frame segment {seg!r}")
        if isinstance(seg, str) and seg.isascii() and seg.isdigit():
            seg = int(seg)
        if isinstance(seg, int) and seg < 0:
            raise ValueError(f"bad frame index {seg}")
        if seg == "":
            raise ValueError("empty frame name")
        out.append(seg)
    return out


def _fill(template_name, values):
    """A com_*.js template without its comment lines, its placeholders replaced in one pass."""
    template = _STRIP_COMMENTS.sub("", js.load(template_name))
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)], template)


def build_request(body, frame, args, helpers):
    """The script that runs ``body`` in ``frame`` with ``args`` as its arguments.

    The request (com_run.js) turns the body into a function with ``new Function`` and stores
    the result in ``__iemcp_r``/``__iemcp_e`` of the top window. For a frame, com_frame.js
    finds the frame's window and runs that same request inside it.
    """
    prelude = _STRIP_COMMENTS.sub("", js.load("com_prelude.js")) if helpers else ""
    path = frame_path(frame)
    run = _fill("com_run.js", {
        "TARGET": "window.top" if path else "window",
        "FUNC": js_literal(prelude + body + "\n"),
        "ARGS": ",".join(js_literal(a) for a in args),
    })
    if not path:
        return run
    return _fill("com_frame.js", {
        "PATH": "[" + ",".join(js_literal(seg) for seg in path) + "]",
        "RUN": js_literal(run),
    })


# --- raw COM access (every call goes through htmldoc.com_call; tests patch these) ------------
def _pythoncom():
    try:
        import pythoncom
    except ImportError as exc:
        raise RuntimeError(htmldoc._NO_PYWIN32) from exc
    return pythoncom


def _com(what, fn, *args):
    """``htmldoc.com_call(fn, *args)`` with a failure that says what was being done."""
    try:
        return htmldoc.com_call(fn, *args)
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"COM call failed ({what}): {exc}") from exc


def _dispid(obj, name):
    dispid = _com(f"look up {name}", obj.GetIDsOfNames, name)
    return dispid[0] if isinstance(dispid, (tuple, list)) else dispid


def _com_get(obj, name):
    """A property of a raw IDispatch (D7: raw GetIDsOfNames + Invoke, never getattr)."""
    pc = _pythoncom()
    return _com(f"read {name}", obj.Invoke, _dispid(obj, name), htmldoc.LOCALE_USER_DEFAULT,
                pc.DISPATCH_PROPERTYGET, True)


def _com_exec(window, source):
    """``window.execScript(source)`` (D7: the script path measured in documentModes 7 and 11)."""
    pc = _pythoncom()
    _com("execScript", window.Invoke, _dispid(window, "execScript"),
         htmldoc.LOCALE_USER_DEFAULT, pc.DISPATCH_METHOD, False, source)


def _com_read(window, name):
    """A window expando (D7: IDispatchEx + InvokeEx; getattr fails in documentMode 11)."""
    pc = _pythoncom()
    ex = _com(f"query IDispatchEx for {name}", window.QueryInterface, pc.IID_IDispatchEx)
    dispid = _com(f"look up {name}", ex.GetDispID, name, 1)  # 1 = fdexNameCaseSensitive
    return _com(f"read {name}", ex.InvokeEx, dispid, 0, pc.DISPATCH_PROPERTYGET, ())


# Posted keys (D7). The lParam follows the spike: repeat count 1, the scan code, and for the
# key-up message the previous-state and transition bits.
WM_KEYDOWN, WM_KEYUP = 0x100, 0x101
_NAMED_KEYS = {
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "escape": 0x1B, "esc": 0x1B, "space": 0x20,
    "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "up": 0x26, "down": 0x28, "left": 0x25,
    "right": 0x27, "arrowup": 0x26, "arrowdown": 0x28, "arrowleft": 0x25, "arrowright": 0x27,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22, "f5": 0x74, "insert": 0x2D,
}
# Keys of the extended block (arrows, navigation, Insert, Delete): the keyboard driver sets bit 24
# of lParam for them, and a posted message should look the same.
_EXTENDED_VKS = frozenset({0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E})


def _post_message(hwnd, msg, wparam, lparam):
    try:
        import win32gui
    except ImportError as exc:
        raise RuntimeError(htmldoc._NO_PYWIN32) from exc
    win32gui.PostMessage(hwnd, msg, wparam, lparam)


def _scan_code(vk):
    try:
        import win32api
    except ImportError as exc:
        raise RuntimeError(htmldoc._NO_PYWIN32) from exc
    return win32api.MapVirtualKey(vk, 0)


def _char_vk(ch):
    """The virtual-key code that types ``ch`` (without Shift: an upper-case letter is typed as lower)."""
    try:
        import win32api
    except ImportError as exc:
        raise RuntimeError(htmldoc._NO_PYWIN32) from exc
    code = win32api.VkKeyScan(ch)
    if code == -1:
        raise ValueError(f"no key types {ch!r} on this keyboard layout")
    return code & 0xFF


def _key_lparams(vk):
    down = 1 | (_scan_code(vk) << 16)
    if vk in _EXTENDED_VKS:
        down |= 1 << 24
    return down, down | (1 << 30) | (1 << 31)


class ComBackend:
    """Reads and acts on an IE document straight through COM (see the module docstring).

    ``hwnd`` is a window that holds the document: ``kind="hwnd"`` any window that is or
    contains an ``Internet Explorer_Server`` (a modal dialog's frame, the main window), and
    ``kind="tab"`` an attached tab, found again from ``ATTACHED_CHAINS`` on every call.
    """

    def __init__(self, hwnd, kind="hwnd"):
        self.hwnd = int(hwnd)
        self.kind = kind
        self._owner = threading.get_ident()
        self._window_obj = None
        self._server = None  # the IE server hwnd the document came from (to see it close)

    # -- the document --
    def server_hwnd(self):
        """The ``Internet Explorer_Server`` window to read, resolved afresh (it can change)."""
        if self.kind != "tab":
            server = htmldoc.ie_server_child(self.hwnd)
            if server is None:
                raise RuntimeError(f"window {self.hwnd} has no visible Internet Explorer_Server "
                                   "window (is it a browser or dialog window?)")
            return server
        # D15: the tab's own IE server hwnd survives navigation, so normally it is still it.
        if htmldoc.window_class(self.hwnd) == IE_SERVER_CLASS:
            return self.hwnd
        if TAB_ANCESTOR_CLASS:
            for ancestor in ATTACHED_CHAINS.get(self.hwnd, []):
                if htmldoc.window_class(ancestor) == TAB_ANCESTOR_CLASS:
                    # Hidden servers count: an inactive tab's is hidden. Zero or several is
                    # ambiguous, and guessing could land in another of the user's tabs.
                    servers = htmldoc.ie_server_descendants(ancestor)
                    if len(servers) == 1:
                        return servers[0]
                    break
        # Never fall back to the top window: it may hold several tabs.
        raise RuntimeError("the attached tab is gone; call ie_tabs and ie_attach again")

    def _window(self):
        if threading.get_ident() != self._owner:
            raise RuntimeError("a ComBackend can only be used by the thread that created it "
                               "(COM objects must not cross threads)")
        if self._window_obj is None:
            self._server = self.server_hwnd()
            document = htmldoc.document_from_hwnd(self._server)
            self._window_obj = _com_get(document, "parentWindow")
        return self._window_obj

    def ensure(self):
        """Nothing to start: the window already exists."""

    def refresh(self):
        """Drop the cached document; the next operation reads the window again (wait loops)."""
        self._window_obj = None

    def reset_frame(self):
        """Nothing to reset: every request names its frame."""

    def close_window(self):
        """``parentWindow.close()`` on the window's document (spike D2: COM reaches dialogs).

        Called straight through the raw dispatch, not as script: after it the window is gone,
        so no result could be read back from it. The page's own ``onbeforeunload`` or IE's
        "close this window?" prompt can still keep it open: the caller checks that.
        """
        window = self._window()
        pc = _pythoncom()
        _com("close the window", window.Invoke, _dispid(window, "close"),
             htmldoc.LOCALE_USER_DEFAULT, pc.DISPATCH_METHOD, False)
        self._window_obj = None  # the document is going away: never reuse it

    # -- running script --
    def _run(self, body, frame=None, args=(), helpers=False):
        request = build_request(body, frame, list(args), helpers)  # validates before COM
        window = self._window()
        _com_exec(window, request)
        try:
            try:
                error = _com_read(window, "__iemcp_e")
            except RuntimeError as exc:
                if self._window_closed():
                    raise WindowClosed("the window closed while the script ran") from exc
                raise RuntimeError("the page did not run the script (it navigated or scripting "
                                   f"is off): {exc}") from exc
            if error:
                raise RuntimeError(str(error))
            result = _com_read(window, "__iemcp_r")
        finally:
            self._clear(window)
        # JScript numbers arrive as doubles: 11 would read back as 11.0.
        if isinstance(result, float) and result.is_integer():
            return int(result)
        return result

    def _window_closed(self, wait=0.5):
        """True when the window this backend reads is gone (checked for ``wait`` seconds).

        Quirk (seen live): a click queued on the page's timer can close its own window (a
        dialog's OK button) before the caller reads the result back, and the read then fails
        with "Member not found". That is the click working, not the page refusing the script.
        """
        server = self._server
        if server is None:
            return False
        deadline = time.monotonic() + wait
        while messages.is_window(server):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
        return True

    @staticmethod
    def _clear(window):
        """Empty the two expandos once they are read: ``__iemcp_r`` can hold page text or a field
        value, which page script could otherwise read (and they would linger until the next call)."""
        try:
            _com_exec(window, CLEAR_SCRIPT)
        except RuntimeError as exc:
            log(f"could not clear the COM result expandos: {exc}")

    def _op(self, el, body, extra=()):
        return self._run(body, el.frame, [el.by, el.value, *extra], helpers=True)

    def _document_mode(self, frame=None):
        try:
            return int(self._run(DOC_MODE_BODY, frame) or 0)
        except (TypeError, ValueError):
            return 0

    # -- the Backend interface --
    def html(self, frame):
        value = self._run(_HTML_BODY, frame)
        return "" if value is None else str(value)

    def text(self, frame):
        value = self._run(_TEXT_BODY, frame)
        return "" if value is None else str(value)

    def js(self, script, frame, args):
        """Run the agent's script as the body of a function (``arguments`` as passed).

        Primitive results come back as they are, arrays joined with the record separator, any
        other object as its string form. Syntax errors are caught in the page (see com_run.js).
        """
        return self._run(script, frame, args)

    def find(self, by, value, frame, timeout, interval=0.5):
        if by == "xpath":
            raise ValueError("xpath is not supported for COM targets")
        if by not in COM_LOCATORS:
            raise ValueError(f"unknown 'by': {by} (COM targets support {list(COM_LOCATORS)})")
        deadline = time.monotonic() + max(0, timeout)
        polls, last_error = 0, None
        while True:
            SESSION.check_cancel()
            if polls:
                self.refresh()  # a navigation during the wait is seen
            polls += 1
            try:
                if by == "css" and self._document_mode(frame) < 8:
                    raise ValueError("css locators need documentMode 8 or later "
                                     "(querySelector does not exist in this document)")
                if self._run(_FIND_BODY, frame, [by, value], helpers=True):
                    return ComElement(by=by, value=value, frame=frame)
                last_error = None
            except RuntimeError as exc:
                last_error = exc  # no document yet, rejected call, ...: not ready, keep polling
            if time.monotonic() >= deadline:
                what = f"no element {by}={value!r} (frame={frame}) within {timeout}s"
                if last_error is not None:
                    raise RuntimeError(f"{what}: {last_error}") from last_error
                raise NoSuchElement(what)
            SESSION.pause(interval)

    def queues_click(self, action, mode):
        return action == "click"  # always: a synchronous click could hang the call (modals)

    def click(self, el, action, mode):
        if action == "click":
            body = _CLICK_BODY
        elif action == "double":
            body = _EVENT_CLICK_BODY % "ondblclick"
        elif action == "right":
            body = _EVENT_CLICK_BODY % "oncontextmenu"
        else:
            raise ValueError("action must be click|double|right")
        try:
            self._op(el, body)
        except WindowClosed:
            pass  # the click closed its own window (a dialog's OK/Cancel): it worked

    def fill(self, el, text, clear):
        self._events_ok(self._op(el, _FILL_BODY, [str(text), bool(clear)]), "fill",
                        "the value was set")

    def select(self, el, how, val):
        result = self._op(el, _SELECT_BODY % js.load("select.js"), [how, str(val)])
        if result == "NOTFOUND":
            raise OptionNotFound(f"option {how}={val!r} not found")
        return str(result)[3:]

    def get(self, el, attr):
        if attr in _GET_BODIES:
            value = self._op(el, _GET_BODIES[attr])
        else:
            value = self._op(el, _GET_ATTR_BODY, [attr])
        return "" if value is None else str(value)

    def hover(self, el):
        self._events_ok(self._op(el, _HOVER_BODY), "hover", "nothing else happened")

    @staticmethod
    def _events_ok(result, what, note):
        """A script event that could not be dispatched must not look like success."""
        if isinstance(result, str) and result.startswith("EVENTS:"):
            raise RuntimeError(f"{what}: these events could not be fired: {result[7:]} ({note})")

    def press_key(self, el, key, frame):
        name = str(key)
        named = _NAMED_KEYS.get(name.lower())
        if named is None and len(name) != 1:
            raise ValueError(f"unknown key {key!r}: use a name like Enter, Tab, Escape, Up, "
                             "Home, PageDown, F5 or one character")
        if el is not None:
            self._op(el, _FOCUS_BODY)
        elif frame not in (None, ""):
            self._run(_FOCUS_WINDOW_BODY, frame)
        if self._document_mode() >= 7:
            # D7: posted WM_KEYDOWN + WM_KEYUP (no WM_CHAR) to the IE server window gave exactly
            # one keypress and the typed character in documentModes 7 and 11 (WM_CHAR as well
            # typed twice; fireEvent typed nothing). Modes 8-10 and Shift-modified keys are
            # untested.
            vk = named if named is not None else _char_vk(name)
            server = self.server_hwnd()
            down, up = _key_lparams(vk)
            _post_message(server, WM_KEYDOWN, vk, down)
            _post_message(server, WM_KEYUP, vk, up)
        else:
            vk = named if named is not None else ord(name.upper())
            by, value = (el.by, el.value) if el is not None else (None, None)
            self._run(_FIRE_KEY_BODY, el.frame if el is not None else frame,
                      [by, value, vk], helpers=True)

    def scroll(self, frame, el, to, dx, dy):
        if el is not None and not to:
            self._op(el, _ELEMENT + "el.scrollIntoView(true);\nreturn 'ok';")
            return "scrolled into view"
        if to in ("top", "bottom"):
            if el is not None:
                pos = "0" if to == "top" else "el.scrollHeight"
                self._op(el, _ELEMENT + f"el.scrollTop = {pos};\nreturn 'ok';")
                return f"scrolled container to {to}"
            y = "0" if to == "top" else "(document.body ? document.body.scrollHeight : 0)"
            self._run(f"window.scrollTo(0, {y});\nreturn 'ok';", frame)
            return f"scrolled page to {to}"
        self._run("window.scrollBy(arguments[0], arguments[1]);\nreturn 'ok';", frame, [dx, dy])
        return f"scrolled by ({dx},{dy})"

    def grid(self, frame, table_id, headers, limit):
        return build_grid(self._run(js.load("grid.js"), frame, [table_id]), headers, limit)

    def frames(self):
        return parse_frames(self._run(js.load("frames.js")))
