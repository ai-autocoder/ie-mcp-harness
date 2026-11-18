"""MCP stdio protocol: newline-delimited JSON-RPC 2.0, plus the @tool registry.

Tool modules register themselves with ``@tool`` when imported; ``ie_mcp.tools`` imports
all of them. ``handle`` calls the tool function directly (a later task routes it through a
watchdog).
"""
import base64
import json
import traceback
from dataclasses import dataclass
from typing import Callable, Optional

from ie_mcp.session import log

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "ie-mcp", "version": "0.1.0"}
SERVER_INSTRUCTIONS = (
    "IE interaction mode is selected when this MCP process starts. "
    "Use IE_INTERACTION_MODE=background when automation runs on a user's active desktop: "
    "it uses synthetic events and does not force browser focus. Prefer it for navigation, "
    "reading, form entry, and ordinary clicks. If a legacy control, hover, upload, modal, or "
    "ActiveX interaction fails in background mode, restart this MCP with "
    "IE_INTERACTION_MODE=native as the compatibility fallback; native mode may take focus and "
    "interfere with the user's mouse. IE mode is not headless in either mode."
)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    fn: Callable[[dict], dict]
    needs_driver: Callable[[dict], bool]
    budget: Optional[Callable[[dict], float]]


# Insertion-ordered: tools/list reports tools in registration order.
REGISTRY: dict[str, ToolSpec] = {}


def tool(name, description, schema, *, needs_driver=True, budget=None):
    """Register a tool function. A bool ``needs_driver`` is wrapped as ``lambda args: value``."""
    if callable(needs_driver):
        needs = needs_driver
    else:
        value = bool(needs_driver)
        needs = lambda args: value  # noqa: E731

    def register(fn):
        if name in REGISTRY:
            raise ValueError(f"tool already registered: {name}")
        REGISTRY[name] = ToolSpec(name, description, schema, fn, needs, budget)
        return fn

    return register


def text_result(s):
    return {"content": [{"type": "text", "text": s}]}


def image_result(png: bytes):
    b64 = base64.b64encode(png).decode("ascii")
    return {"content": [{"type": "image", "data": b64, "mimeType": "image/png"}]}


def ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def err(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def public_tools():
    return [{"name": t.name, "description": t.description, "inputSchema": t.input_schema}
            for t in REGISTRY.values()]


def handle(msg):
    """Return a response dict, or None for notifications."""
    method = msg.get("method")
    mid = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        client_ver = params.get("protocolVersion", PROTOCOL_VERSION)
        return ok(mid, {
            "protocolVersion": client_ver,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
            "instructions": SERVER_INSTRUCTIONS,
        })
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return ok(mid, {})
    if method == "tools/list":
        return ok(mid, {"tools": public_tools()})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        spec = REGISTRY.get(name)
        if spec is None:
            return err(mid, -32602, f"unknown tool: {name}")
        try:
            result = spec.fn(args)
            return ok(mid, result)
        except Exception as exc:
            log("tool error:", traceback.format_exc())
            return ok(mid, {
                "content": [{"type": "text", "text": f"ERROR in {name}: {exc}"}],
                "isError": True,
            })
    if mid is not None:
        return err(mid, -32601, f"method not found: {method}")
    return None


def serve(stdin, stdout):
    """Read one JSON-RPC message per line from ``stdin``; write one response line to ``stdout``."""
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            log(f"DROP non-JSON line ({len(line)} chars)")
            continue
        log(f"recv id={msg.get('id')} method={msg.get('method')} "
            f"tool={(msg.get('params') or {}).get('name')}")
        resp = handle(msg)
        if resp is not None:
            stdout.write(json.dumps(resp) + "\n")
            stdout.flush()
