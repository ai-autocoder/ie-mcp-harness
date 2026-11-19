"""Tools that act on the page: click, fill, select, keys, hover, scroll, upload, script."""
import json
import time
from pathlib import Path

from ie_mcp import config
from ie_mcp.backends import (OptionNotFound, TARGET_SCHEMA, resolve_backend, target_kind,
                            uses_driver)
from ie_mcp.protocol import locator_budget, text_result, tool
from ie_mcp.session import SESSION


# Post-action pauses; each tool's watchdog budget is computed from the same defaults.
CLICK_WAIT = 2
SELECT_WAIT = 2
PRESS_KEY_WAIT = 1
HOVER_WAIT = 1


def _click_mode(args):
    return args.get("mode") or config.SETTINGS.click_mode


def _click_is_deferred(args):
    """An async plain click only queues e.click(); double/right always use the driver.

    Over COM a plain click is always queued (``ComBackend.queues_click``), so it defers its
    effect whatever the click mode; double/right fire their events at once.
    """
    plain = args.get("action", "click") == "click"
    if target_kind(args.get("target")) != "driver":
        return plain
    return plain and _click_mode(args) == "async"


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
    "the driver. Over COM (target hwnd:/tab:) a click is always queued on the page's timer and "
    "synthetic (script events, not mouse input).",
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
            "target": TARGET_SCHEMA,
        },
        "required": ["by", "value"],
    },
    budget=locator_budget(CLICK_WAIT),
    defers_effect=_click_is_deferred,
    needs_driver=uses_driver,
)
def t_click(args):
    action = args.get("action", "click")
    mode = _click_mode(args)
    # Validate before touching the browser: a bad argument must fail without side effects.
    if action not in ("click", "double", "right"):
        raise ValueError("action must be click|double|right")
    if mode not in config.CLICK_MODES:
        raise ValueError("mode must be driver|async")
    backend = resolve_backend(args.get("target"))
    backend.ensure()
    el = backend.find(args["by"], args["value"], args.get("frame"), args.get("timeout", 10))
    backend.click(el, action, mode)
    time.sleep(args.get("wait", CLICK_WAIT))
    queued = (" (async: queued, runs after this call returns)"
              if backend.queues_click(action, mode) else "")
    return text_result(f"{action}-clicked {args['by']}={args['value']}{queued}")


@tool(
    "ie_fill",
    "Type text into an input/textarea element (auto-waits). Over COM (target hwnd:/tab:) the "
    "value is set by script and key/change/blur events are fired: synthetic, not keystrokes.",
    {
        "type": "object",
        "properties": {
            "by": {"type": "string"},
            "value": {"type": "string"},
            "text": {"type": "string"},
            "frame": {"type": ["string", "integer"]},
            "clear": {"type": "boolean"},
            "timeout": {"type": "number", "description": "seconds to wait for the element (default 10)"},
            "target": TARGET_SCHEMA,
        },
        "required": ["by", "value", "text"],
    },
    budget=locator_budget(),
    needs_driver=uses_driver,
)
def t_fill(args):
    backend = resolve_backend(args.get("target"))
    backend.ensure()
    el = backend.find(args["by"], args["value"], args.get("frame"), args.get("timeout", 10))
    backend.fill(el, args["text"], args.get("clear", True))
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
            "target": TARGET_SCHEMA,
        },
        "required": ["by", "value"],
    },
    budget=locator_budget(SELECT_WAIT),
    needs_driver=uses_driver,
)
def t_select(args):
    """Select an <option> by label/value/index, set via JS + fire onchange.

    (Selenium's Select clicks the <option>, which the legacy Trident engine
    rejects with "Error executing JavaScript" — so we set it directly.)
    """
    backend = resolve_backend(args.get("target"))
    backend.ensure()
    el = backend.find(args["by"], args["value"], args.get("frame"), args.get("timeout", 10))
    if "label" in args:
        mode, val = "label", args["label"]
    elif "option_value" in args:
        mode, val = "value", args["option_value"]
    elif "index" in args:
        mode, val = "index", str(args["index"])
    else:
        raise ValueError("provide one of: label, option_value, index")
    try:
        chosen = backend.select(el, mode, val)
    except OptionNotFound:
        raise ValueError(f"option {mode}={val!r} not found in {args['by']}={args['value']}") from None
    time.sleep(args.get("wait", SELECT_WAIT))
    return text_result(f"selected {mode}={val} -> {chosen}")


@tool(
    "ie_press_key",
    "Press a key on an element (if by/value given) or the active element. "
    "Key names: Enter, Tab, Escape, Space, Backspace, Delete, Up/Down/Left/Right, "
    "Home, End, PageUp, PageDown, F5, or a literal character. Over COM (target hwnd:/tab:) "
    "the key is posted to the window as WM_KEYDOWN/WM_KEYUP (documentMode 7+), so Shift-modified "
    "characters are typed unshifted.",
    {
        "type": "object",
        "properties": {
            "key": {"type": "string"},
            "by": {"type": "string"},
            "value": {"type": "string"},
            "frame": {"type": ["string", "integer"]},
            "wait": {"type": "number"},
            "timeout": {"type": "number", "description": "seconds to wait for the element (default 10)"},
            "target": TARGET_SCHEMA,
        },
        "required": ["key"],
    },
    budget=locator_budget(PRESS_KEY_WAIT),
    needs_driver=uses_driver,
)
def t_press_key(args):
    """Press a key (Enter/Tab/Escape/arrows/etc. or a literal char) on an element or the page."""
    backend = resolve_backend(args.get("target"))
    backend.ensure()
    if args.get("value"):
        el = backend.find(args["by"], args["value"], args.get("frame"), args.get("timeout", 10))
        backend.press_key(el, args["key"], args.get("frame"))
        where = f"{args['by']}={args['value']}"
    else:
        backend.press_key(None, args["key"], args.get("frame"))
        where = "active element"
    time.sleep(args.get("wait", PRESS_KEY_WAIT))
    return text_result(f"pressed {args['key']} on {where}")


@tool(
    "ie_hover",
    "Hover the mouse over an element (for menus/tooltips). Over COM (target hwnd:/tab:) "
    "mouseover and mouseenter events are fired by script: synthetic, the mouse does not move.",
    {
        "type": "object",
        "properties": {
            "by": {"type": "string"},
            "value": {"type": "string"},
            "frame": {"type": ["string", "integer"]},
            "wait": {"type": "number"},
            "timeout": {"type": "number", "description": "seconds to wait for the element (default 10)"},
            "target": TARGET_SCHEMA,
        },
        "required": ["by", "value"],
    },
    budget=locator_budget(HOVER_WAIT),
    needs_driver=uses_driver,
)
def t_hover(args):
    backend = resolve_backend(args.get("target"))
    backend.ensure()
    el = backend.find(args["by"], args["value"], args.get("frame"), args.get("timeout", 10))
    backend.hover(el)
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
            "target": TARGET_SCHEMA,
        },
    },
    budget=locator_budget(),
    needs_driver=uses_driver,
)
def t_scroll(args):
    """Scroll: bring an element into view (by/value), or scroll the page/a container.

    Modes:
      - by/value given      -> element.scrollIntoView()
      - to = top|bottom     -> scroll window (or container by/value) to that edge
      - dx/dy given         -> window.scrollBy(dx, dy)
    """
    backend = resolve_backend(args.get("target"))
    backend.ensure()
    frame = args.get("frame")
    to = args.get("to")
    el = None
    if args.get("value"):
        el = backend.find(args["by"], args["value"], frame, args.get("timeout", 10))
    message = backend.scroll(frame, el, to, int(args.get("dx", 0)), int(args.get("dy", 0)))
    if el is not None and not to:
        message += f": {args['by']}={args['value']}"
    return text_result(message)


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
    "Execute JavaScript in the page or a frame and return the result. Over COM (target "
    "hwnd:/tab:) the script runs as a function body in the frame's own window: write ES3 "
    "(no JSON, let, arrow functions), return a primitive or an array (arrays come back joined "
    "with the character U+001E); a script error is reported, never sent to the page's onerror.",
    {
        "type": "object",
        "properties": {
            "script": {"type": "string"},
            "frame": {"type": ["string", "integer"]},
            "limit": {"type": "integer"},
            "target": TARGET_SCHEMA,
        },
        "required": ["script"],
    },
    needs_driver=uses_driver,
)
def t_js(args):
    backend = resolve_backend(args.get("target"))
    backend.ensure()
    res = backend.js(args["script"], args.get("frame"), [])
    return text_result(json.dumps(res, default=str)[: args.get("limit", 8000)])
