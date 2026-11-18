"""Diagnostics: session status, browser listing, orphan cleanup, sleep."""
import json
import time

from ie_mcp import config
from ie_mcp.protocol import text_result, tool
from ie_mcp.session import SESSION, cleanup_stale_sessions, list_ie_browsers


@tool(
    "ie_status",
    "Session state as JSON: active/alive, title, url, window_handles, and the "
    "interaction mode, session identity/directory, and owned browser PIDs/profile.",
    {"type": "object", "properties": {}},
)
def t_status(args):
    info = {
        "active": SESSION.driver is not None,
        "interaction_mode": config.SETTINGS.interaction_mode.name,
        "session_id": config.SETTINGS.session_paths.session_id,
        "session_directory": str(config.SETTINGS.session_paths.directory),
        "driver_pid": SESSION.driver_pid,
        "edge_pid": SESSION.edge_pid,
        "profile": SESSION.profile,
    }
    if SESSION.driver is None:
        info["alive"] = False
        return text_result(json.dumps(info, indent=2))
    try:
        d = SESSION.driver
        info["alive"] = True
        info["title"] = d.title
        info["url"] = d.current_url
        try:
            info["window_handles"] = len(d.window_handles)
        except Exception:
            pass
    except Exception as exc:
        info["alive"] = False
        info["error"] = str(exc).splitlines()[0]
    return text_result(json.dumps(info, indent=2))


@tool(
    "ie_browsers",
    "List ALL Edge IE-mode windows and IEDriver processes open on the machine, "
    "flagging which one THIS session owns (mine=true) and which are orphans. "
    "Use to see leftover browsers from prior/other runs.",
    {"type": "object", "properties": {}},
    needs_driver=False,
)
def t_browsers(args):
    """List all Edge IE-mode windows + IEDriver processes, flagging the one we own."""
    rows = list_ie_browsers()
    for b in rows:
        b["mine"] = (b["kind"] == "edge-ie" and b["pid"] == SESSION.edge_pid) or \
                    (b["kind"] == "iedriver" and b["pid"] == SESSION.driver_pid)
    edges = [b for b in rows if b["kind"] == "edge-ie"]
    other_edges = [b["pid"] for b in edges if not b["mine"]]
    return text_result(json.dumps({
        "current": {"session_id": config.SETTINGS.session_paths.session_id, "driver_pid": SESSION.driver_pid,
                    "edge_pid": SESSION.edge_pid, "profile": SESSION.profile},
        "ie_browsers": edges,
        "iedriver_processes": [b["pid"] for b in rows if b["kind"] == "iedriver"],
        "other_agent_or_untracked_edge_pids": other_edges,
    }, indent=2))


@tool(
    "ie_kill_orphans",
    "Terminate only IE-mode Edge / IEDriver processes recorded for crashed ie-mcp "
    "sessions. Never kills browsers owned by live agents or unknown processes.",
    {"type": "object", "properties": {}},
    needs_driver=False,
)
def t_kill_orphans(args):
    """Kill processes owned by crashed sessions, preserving every live agent."""
    result = cleanup_stale_sessions(config.SETTINGS.session_paths.root)
    result["kept_current"] = SESSION.edge_pid
    return text_result(json.dumps(result, indent=2))


@tool(
    "ie_sleep",
    "Sleep for N seconds (last resort; prefer ie_wait_ready / ie_wait_text).",
    {
        "type": "object",
        "properties": {"seconds": {"type": "number"}},
    },
    needs_driver=False,
)
def t_sleep(args):
    secs = float(args.get("seconds", 1))
    time.sleep(secs)
    return text_result(f"slept {secs}s")
