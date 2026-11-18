"""Tools that act on the page: click, fill, select, keys, hover, scroll, upload, script."""
import json
import time

from ie_mcp import js
from ie_mcp.protocol import text_result, tool
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


@tool(
    "ie_click",
    "Click an element (auto-waits). 'by' is id/css/xpath/name/link_text/tag/class. "
    "action: click (default) | double | right.",
    {
        "type": "object",
        "properties": {
            "by": {"type": "string"},
            "value": {"type": "string"},
            "action": {"type": "string", "description": "click | double | right"},
            "frame": {"type": ["string", "integer"]},
            "timeout": {"type": "number"},
            "wait": {"type": "number"},
        },
        "required": ["by", "value"],
    },
)
def t_click(args):
    el = SESSION.find(args["by"], args["value"], args.get("frame"), args.get("timeout", 10))
    action = args.get("action", "click")
    if action == "click":
        el.click()
    else:
        from selenium.webdriver.common.action_chains import ActionChains
        ac = ActionChains(SESSION.driver)
        if action == "double":
            ac.double_click(el).perform()
        elif action == "right":
            ac.context_click(el).perform()
        else:
            raise ValueError("action must be click|double|right")
    time.sleep(args.get("wait", 2))
    return text_result(f"{action}-clicked {args['by']}={args['value']}")


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
        },
        "required": ["by", "value", "text"],
    },
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
        },
        "required": ["by", "value"],
    },
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
    time.sleep(args.get("wait", 2))
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
        },
        "required": ["key"],
    },
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
    time.sleep(args.get("wait", 1))
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
        },
        "required": ["by", "value"],
    },
)
def t_hover(args):
    from selenium.webdriver.common.action_chains import ActionChains
    d = SESSION.ensure()
    el = SESSION.find(args["by"], args["value"], args.get("frame"), args.get("timeout", 10))
    ActionChains(d).move_to_element(el).perform()
    time.sleep(args.get("wait", 1))
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
        },
    },
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


@tool(
    "ie_upload",
    "Set a file <input type=file> by sending a local file path.",
    {
        "type": "object",
        "properties": {
            "by": {"type": "string"},
            "value": {"type": "string"},
            "path": {"type": "string", "description": "absolute local file path"},
            "frame": {"type": ["string", "integer"]},
        },
        "required": ["by", "value", "path"],
    },
)
def t_upload(args):
    """Set a file <input type=file> by sending the local file path."""
    el = SESSION.find(args["by"], args["value"], args.get("frame"), args.get("timeout", 10))
    el.send_keys(args["path"])
    return text_result(f"uploaded {args['path']} -> {args['by']}={args['value']}")


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
