"""Diagnostics: session status, browser listing, orphan cleanup, sleep."""
import json
import time

from ie_mcp import config
from ie_mcp.protocol import driver_hold, number_arg, text_result, tool
from ie_mcp.session import SESSION, cleanup_stale_sessions, list_ie_browsers
from ie_mcp.tools import console


SLEEP_DEFAULT = 1  # seconds, for ie_sleep and its watchdog budget
STATUS_LOCK_WAIT = 0.5  # seconds ie_status waits for the driver before reporting "busy"


@tool(
    "ie_status",
    "Session state as JSON: active/alive, title, url, window_handles, and the "
    "interaction mode, session identity/directory, owned browser PIDs/profile, and the page "
    "modifications: instrumented (wrappers installed), console_stub, xhr_wrapped. "
    "Never waits for a busy browser: while another call holds it, title/url/window_handles "
    "are left out and \"driver\": \"busy\" is reported.",
    {"type": "object", "properties": {}},
    needs_driver=False,
    # It waits STATUS_LOCK_WAIT for the lock itself, so a tiny IE_TOOL_TIMEOUT must not make
    # the watchdog answer first.
    budget=lambda args: STATUS_LOCK_WAIT + 5,
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
        # Page modifications (R13): wrappers are installed once a hook has run; xhr_wrapped is
        # the flag last sent to a page (or what the next page will get).
        "instrumented": console.is_instrumented(),
        "console_stub": bool(config.SETTINGS.instrument and config.SETTINGS.console_stub),
        "xhr_wrapped": bool(config.SETTINGS.instrument and (
            console.LAST_FLAGS or console.page_flags())["wrap_xhr"]),
    }
    if SESSION.driver is None:
        info["alive"] = False
        return text_result(json.dumps(info, indent=2))
    # ie_status does not take the driver lock through the watchdog (that would make it wait
    # for, or fail behind, the very call a stuck session needs diagnosing). It asks for the
    # lock briefly itself and says "busy" if it cannot have it: the driver must not be used
    # concurrently with the call that holds it.
    # driver_hold registers ie_status as the holder, so a BUSY answer to another call names it.
    with driver_hold("ie_status", STATUS_LOCK_WAIT) as got:
        if not got:
            info["driver"] = "busy"
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
    budget=lambda args: number_arg(args, "seconds", SLEEP_DEFAULT) + 5,
)
def t_sleep(args):
    secs = float(args.get("seconds", SLEEP_DEFAULT))
    time.sleep(secs)
    return text_result(f"slept {secs}s")
