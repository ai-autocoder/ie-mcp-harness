"""Navigation tools: open, goto, back, forward, resize, close."""
import time

from ie_mcp import config
from ie_mcp.protocol import text_result, tool
from ie_mcp.session import SESSION, log


@tool(
    "ie_open",
    "Start (or reuse) an Edge IE-mode session and navigate to a URL "
    "(default IE_DEFAULT_URL). Keeps the session alive for later calls. An http(s) URL is "
    "required in IE mode: about:blank renders in Chromium and IEDriver cannot find an IE "
    "window, so a new session opened on it fails.",
    {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": f"URL to open (default {config.SETTINGS.default_url})"},
            "wait": {"type": "number", "description": "seconds to wait after load (default 4)"},
        },
    },
)
def t_open(args):
    url = args.get("url") or config.SETTINGS.default_url
    fresh = SESSION.driver is None
    # New session: open url AS PART OF creation (crosses PM boundary safely).
    d = SESSION.ensure(initial_url=url)
    if not fresh:
        # Existing session: a normal navigation is fine within the same zone.
        try:
            d.get(url)
        except Exception as exc:
            log("get() note:", exc)
    time.sleep(args.get("wait", 4))
    return text_result(f"opened {url}\ntitle: {d.title}\nurl: {d.current_url}")


@tool(
    "ie_goto",
    "Navigate the current IE-mode session to a new URL.",
    {
        "type": "object",
        "properties": {"url": {"type": "string"}, "wait": {"type": "number"}},
        "required": ["url"],
    },
)
def t_goto(args):
    url = args["url"]
    d = SESSION.ensure()
    try:
        d.get(url)
    except Exception as exc:
        log("get() note:", exc)
    time.sleep(args.get("wait", 3))
    return text_result(f"title: {d.title}\nurl: {d.current_url}")


@tool(
    "ie_back",
    "Navigate back in browser history.",
    {"type": "object", "properties": {"wait": {"type": "number"}}},
)
def t_back(args):
    d = SESSION.ensure()
    d.back()
    time.sleep(args.get("wait", 2))
    return text_result(f"back -> {d.current_url}")


@tool(
    "ie_forward",
    "Navigate forward in browser history.",
    {"type": "object", "properties": {"wait": {"type": "number"}}},
)
def t_forward(args):
    d = SESSION.ensure()
    d.forward()
    time.sleep(args.get("wait", 2))
    return text_result(f"forward -> {d.current_url}")


@tool(
    "ie_resize",
    "Resize the browser window (useful for consistent screenshots).",
    {
        "type": "object",
        "properties": {"width": {"type": "integer"}, "height": {"type": "integer"}},
        "required": ["width", "height"],
    },
)
def t_resize(args):
    d = SESSION.ensure()
    d.set_window_size(int(args["width"]), int(args["height"]))
    return text_result(f"resized to {args['width']}x{args['height']}")


@tool(
    "ie_close",
    "Close the IE-mode session and quit the browser. The session name stays reserved for "
    "this server; ie_open starts a new browser. With force=true, running waits are cancelled "
    "and this session's own IEDriver and Edge are killed (only if they are still the "
    "recorded processes) without calling the driver at all: use it when a call hangs or the "
    "page is stuck in a dialog.",
    {
        "type": "object",
        "properties": {
            "force": {"type": "boolean",
                      "description": "cancel waits and kill the browser without asking the driver (default false)"},
        },
    },
    needs_driver=lambda args: not args.get("force"),
)
def t_close(args):
    if args.get("force"):
        killed = SESSION.force_kill()
        return text_result(f"session force-closed; killed pids: {killed}")
    SESSION.quit_browser()
    return text_result("session closed")
