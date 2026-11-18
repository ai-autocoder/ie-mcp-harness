"""Tools that act on the page: click, fill, select, keys, hover, scroll, upload, script."""
import json
import time
from pathlib import Path

from ie_mcp import config, js
from ie_mcp.protocol import locator_budget, text_result, tool
from ie_mcp.session import SESSION


_KEY_MAP = None


def _key(name):
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
            "pagedown": Keys.PAGE_DOWN, "f5": Keys.F5,
        }
    return _KEY_MAP.get(name.lower(), name)


# Post-action pauses; each tool's watchdog budget is computed from the same defaults.
CLICK_WAIT = 2
SELECT_WAIT = 2
PRESS_KEY_WAIT = 1
HOVER_WAIT = 1

# Queue the click on the page's own timer without waiting for its handlers. A WebDriver click whose handler
# opens alert/confirm/showModalDialog can block until that dialog closes; a timer callback
# cannot block this call. The price: the click is script-initiated (see the tool description).
ASYNC_CLICK_SCRIPT = ("var e=arguments[0];window.setTimeout(function(){e.click();},0);"
                      "return 'queued';")


def _click_mode(args):
    return args.get("mode") or config.SETTINGS.click_mode


def _click_is_deferred(args):
    """An async plain click only queues e.click(); double/right always use the driver."""
    return args.get("action", "click") == "click" and _click_mode(args) == "async"


@tool(
    "ie_click",
    "Click an element (auto-waits). 'by' is id/css/xpath/name/link_text/tag/class. "
    "action: click (default) | double | right. "
    "mode: driver (a WebDriver click; the default unless IE_CLICK_MODE=async) | async. "
    "An async click is queued on the page's own timer, so the call does not wait for the "
    "click's handlers (only the usual 'wait' pause follows) and cannot block even if the "
    "click opens a dialog that would block a driver click. It is script-initiated: there is "
    "no mousedown/mouseup/focus, handlers that test for a user gesture can behave differently, "
    "and the pop-up blocker may block a window.open it triggers. double and right always use "
    "the driver.",
    {
        "type": "object",
        "properties": {
            "by": {"type": "string"},
            "value": {"type": "string"},
            "action": {"type": "string", "description": "click | double | right"},
            "mode": {"type": "string", "description": "driver | async (default: IE_CLICK_MODE, normally driver)"},
            "frame": {"type": ["string", "integer"]},
            "timeout": {"type": "number", "description": "seconds to wait for the element (default 10)"},
            "wait": {"type": "number", "description": "seconds to pause after the click (default 2)"},
        },
        "required": ["by", "value"],
    },
    budget=locator_budget(CLICK_WAIT),
    defers_effect=_click_is_deferred,
)
def t_click(args):
    action = args.get("action", "click")
    mode = _click_mode(args)
    # Validate before touching the browser: a bad argument must fail without side effects.
    if action not in ("click", "double", "right"):
        raise ValueError("action must be click|double|right")
    if mode not in config.CLICK_MODES:
        raise ValueError("mode must be driver|async")
    el = SESSION.find(args["by"], args["value"], args.get("frame"), args.get("timeout", 10))
    if action == "click" and mode == "async":
        SESSION.ensure().execute_script(ASYNC_CLICK_SCRIPT, el)
    elif action == "click":
        el.click()
    else:
        from selenium.webdriver.common.action_chains import ActionChains
        ac = ActionChains(SESSION.driver)
        if action == "double":
            ac.double_click(el).perform()
        else:
            ac.context_click(el).perform()
    time.sleep(args.get("wait", CLICK_WAIT))
    queued = " (async: queued, runs after this call returns)" if action == "click" and mode == "async" else ""
    return text_result(f"{action}-clicked {args['by']}={args['value']}{queued}")


@tool(
    "ie_fill",
    "Type text into an input/textarea element (auto-waits).",
    {
        "type": "object",
        "properties": {
            "by": {"type": "string"},
            "value": {"type": "string"},
            "text": {"type": "string"},
            "frame": {"type": ["string", "integer"]},
            "clear": {"type": "boolean"},
            "timeout": {"type": "number", "description": "seconds to wait for the element (default 10)"},
        },
        "required": ["by", "value", "text"],
    },
    budget=locator_budget(),
)
def t_fill(args):
    el = SESSION.find(args["by"], args["value"], args.get("frame"), args.get("timeout", 10))
    if args.get("clear", True):
        el.clear()
    el.send_keys(args["text"])
    return text_result(f"filled {args['by']}={args['value']}")


@tool(
    "ie_select",
    "Select an <option> in a <select> dropdown, by label (visible text), "
    "option_value, or index. Provide exactly one of label/option_value/index.",
    {
        "type": "object",
        "properties": {
            "by": {"type": "string"},
            "value": {"type": "string"},
            "label": {"type": "string", "description": "option visible text"},
            "option_value": {"type": "string", "description": "option value attribute"},
            "index": {"type": "integer", "description": "option index (0-based)"},
            "frame": {"type": ["string", "integer"]},
            "timeout": {"type": "number", "description": "seconds to wait for the element (default 10)"},
            "wait": {"type": "number", "description": "seconds to pause after selecting (default 2)"},
        },
        "required": ["by", "value"],
    },
    budget=locator_budget(SELECT_WAIT),
)
def t_select(args):
    """Select an <option> by label/value/index, set via JS + fire onchange.

    (Selenium's Select clicks the <option>, which the legacy Trident engine
    rejects with "Error executing JavaScript" — so we set it directly.)
    """
    d = SESSION.ensure()
    el = SESSION.find(args["by"], args["value"], args.get("frame"), args.get("timeout", 10))
    if "label" in args:
        mode, val = "label", args["label"]
    elif "option_value" in args:
        mode, val = "value", args["option_value"]
    elif "index" in args:
        mode, val = "index", str(args["index"])
    else:
        raise ValueError("provide one of: label, option_value, index")
    res = d.execute_script(js.load("select.js"), el, mode, val)
    if res == "NOTFOUND":
        raise ValueError(f"option {mode}={val!r} not found in {args['by']}={args['value']}")
    time.sleep(args.get("wait", SELECT_WAIT))
    return text_result(f"selected {mode}={val} -> {res[3:]}")


@tool(
    "ie_press_key",
    "Press a key on an element (if by/value given) or the active element. "
    "Key names: Enter, Tab, Escape, Space, Backspace, Delete, Up/Down/Left/Right, "
    "Home, End, PageUp, PageDown, F5 — or a literal character.",
    {
        "type": "object",
        "properties": {
            "key": {"type": "string"},
            "by": {"type": "string"},
            "value": {"type": "string"},
            "frame": {"type": ["string", "integer"]},
            "wait": {"type": "number"},
            "timeout": {"type": "number", "description": "seconds to wait for the element (default 10)"},
        },
        "required": ["key"],
    },
    budget=locator_budget(PRESS_KEY_WAIT),
)
def t_press_key(args):
    """Press a key (Enter/Tab/Escape/arrows/etc. or a literal char) on an element or the page."""
    d = SESSION.ensure()
    key = _key(args["key"])
    if args.get("value"):
        el = SESSION.find(args["by"], args["value"], args.get("frame"), args.get("timeout", 10))
        el.send_keys(key)
        where = f"{args['by']}={args['value']}"
    else:
        SESSION._apply_frame(args.get("frame"))
        d.switch_to.active_element.send_keys(key)
        where = "active element"
    time.sleep(args.get("wait", PRESS_KEY_WAIT))
    return text_result(f"pressed {args['key']} on {where}")


@tool(
    "ie_hover",
    "Hover the mouse over an element (for menus/tooltips).",
    {
        "type": "object",
        "properties": {
            "by": {"type": "string"},
            "value": {"type": "string"},
            "frame": {"type": ["string", "integer"]},
            "wait": {"type": "number"},
            "timeout": {"type": "number", "description": "seconds to wait for the element (default 10)"},
        },
        "required": ["by", "value"],
    },
    budget=locator_budget(HOVER_WAIT),
)
def t_hover(args):
    from selenium.webdriver.common.action_chains import ActionChains
    d = SESSION.ensure()
    el = SESSION.find(args["by"], args["value"], args.get("frame"), args.get("timeout", 10))
    ActionChains(d).move_to_element(el).perform()
    time.sleep(args.get("wait", HOVER_WAIT))
    return text_result(f"hovered {args['by']}={args['value']}")


@tool(
    "ie_scroll",
    "Scroll: bring an element into view (by/value), scroll page/container to "
    "top|bottom (to=...), or by pixels (dx/dy). Needed for virtualized grids.",
    {
        "type": "object",
        "properties": {
            "by": {"type": "string"},
            "value": {"type": "string"},
            "to": {"type": "string", "description": "top | bottom"},
            "dx": {"type": "integer"},
            "dy": {"type": "integer"},
            "frame": {"type": ["string", "integer"]},
            "timeout": {"type": "number", "description": "seconds to wait for the element (default 10)"},
        },
    },
    budget=locator_budget(),
)
def t_scroll(args):
    """Scroll: bring an element into view (by/value), or scroll the page/a container.

    Modes:
      - by/value given      -> element.scrollIntoView()
      - to = top|bottom     -> scroll window (or container by/value) to that edge
      - dx/dy given         -> window.scrollBy(dx, dy)
    """
    d = SESSION.ensure()
    frame = args.get("frame")
    SESSION._apply_frame(frame)
    to = args.get("to")
    if args.get("value") and not to:
        el = SESSION.find(args["by"], args["value"], frame, args.get("timeout", 10))
        d.execute_script("arguments[0].scrollIntoView(true);", el)
        return text_result(f"scrolled into view: {args['by']}={args['value']}")
    if to in ("top", "bottom"):
        if args.get("value"):
            el = SESSION.find(args["by"], args["value"], frame, args.get("timeout", 10))
            pos = "0" if to == "top" else "arguments[0].scrollHeight"
            d.execute_script(f"arguments[0].scrollTop = {pos};", el)
            return text_result(f"scrolled container to {to}")
        y = "0" if to == "top" else "document.body.scrollHeight"
        d.execute_script(f"window.scrollTo(0, {y});")
        return text_result(f"scrolled page to {to}")
    dx = int(args.get("dx", 0))
    dy = int(args.get("dy", 0))
    d.execute_script("window.scrollBy(arguments[0], arguments[1]);", dx, dy)
    return text_result(f"scrolled by ({dx},{dy})")


def _upload_target(path, upload_dir):
    """Resolve ``path`` (relative paths against ``upload_dir``) to an existing file inside it.

    Uses Path.resolve(), which follows symlinks and junctions and collapses "..", and a
    real containment test (not a string prefix: "uploads-evil" is not inside "uploads").
    """
    if upload_dir is None:
        raise ValueError("uploads are disabled: set IE_UPLOAD_DIR to the directory files "
                         "may be uploaded from")
    base = Path(upload_dir).resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = base / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(base)
    except ValueError:
        raise ValueError(f"path {path!r} is outside IE_UPLOAD_DIR ({base})") from None
    if not resolved.is_file():
        raise ValueError(f"no such file inside IE_UPLOAD_DIR: {resolved}")
    return resolved


@tool(
    "ie_upload",
    "Set a file <input type=file> by sending a local file path. Only files inside the "
    "directory configured with IE_UPLOAD_DIR can be uploaded (disabled when it is unset).",
    {
        "type": "object",
        "properties": {
            "by": {"type": "string"},
            "value": {"type": "string"},
            "path": {"type": "string",
                     "description": "file inside IE_UPLOAD_DIR: relative to it, or absolute"},
            "frame": {"type": ["string", "integer"]},
            "timeout": {"type": "number", "description": "seconds to wait for the element (default 10)"},
        },
        "required": ["by", "value", "path"],
    },
    budget=locator_budget(),
)
def t_upload(args):
    """Set a file <input type=file> by sending the local file path."""
    target = _upload_target(args["path"], config.SETTINGS.upload_dir)
    el = SESSION.find(args["by"], args["value"], args.get("frame"), args.get("timeout", 10))
    el.send_keys(str(target))
    return text_result(f"uploaded {target} -> {args['by']}={args['value']}")


@tool(
    "ie_js",
    "Execute JavaScript in the page or a frame and return the result.",
    {
        "type": "object",
        "properties": {
            "script": {"type": "string"},
            "frame": {"type": ["string", "integer"]},
            "limit": {"type": "integer"},
        },
        "required": ["script"],
    },
)
def t_js(args):
    d = SESSION.ensure()
    SESSION._apply_frame(args.get("frame"))
    res = d.execute_script(args["script"])
    return text_result(json.dumps(res, default=str)[: args.get("limit", 8000)])
