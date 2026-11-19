"""Diagnostics: session status, browser listing, orphan cleanup, sleep."""
import json
import time

from ie_mcp import config, session
from ie_mcp.protocol import driver_hold, number_arg, text_result, tool
from ie_mcp.session import SESSION, cleanup_stale_sessions, list_ie_browsers
from ie_mcp.tools import console


SLEEP_DEFAULT = 1  # seconds, for ie_sleep and its watchdog budget
STATUS_LOCK_WAIT = 0.5  # seconds ie_status waits for the driver before reporting "busy"

ZONE_SENTENCE = ("the proxy can change the IE security zone of intranet hosts; use "
                 "IE_CAPTURE_BYPASS or IE_CAPTURE=0 if a page behaves differently")


def capture_state(settings=None):
    """ie_status ``capture``: off (IE_CAPTURE=0), proxy (verified), page-only (the browser did
    not use the proxy: only the page's XHR wrapper records) or unverified (no http(s)
    navigation since the browser started has been checked yet)."""
    settings = config.SETTINGS if settings is None else settings
    if not settings.capture:
        return "off"
    if SESSION.capture_active is True:
        return "proxy"
    if SESSION.capture_active is False:
        return "page-only"
    return "unverified"


def capture_note(state, settings=None):
    """What the capture does not show (spike D11: popups, modal dialogs and their XMLHTTP are
    proxied; the session's initial URL is not) and the zone risk of the proxy."""
    settings = config.SETTINGS if settings is None else settings
    if settings.capture and settings.capture_upstream_proxy_error:
        return ("network capture cannot start: " + settings.capture_upstream_proxy_error
                + "; the next session start fails until it is fixed")
    if state == "off":
        return ("network capture is off (IE_CAPTURE=0): only XHR recorded by the page "
                "instrumentation is listed (source page); document loads, form posts and "
                "frames are not captured")
    if state == "page-only":
        reason = f" ({SESSION.capture_error})" if SESSION.capture_error else ""
        return ("network capture is off: the browser did not use the proxy" + reason + "; only "
                "XHR recorded by the page instrumentation is listed (source page)")
    note = ("not captured: the session's initial URL (the browser loads it before the proxy "
            "applies), fresh browser-cache hits (IE_CAPTURE_NO_CACHE=1 turns revalidations into "
            "full fetches) and the content of HTTPS connections (only the tunnel: host, "
            "timing); ")
    if settings.capture_bypass:
        # A bypassed host never reaches the proxy: no capture, no loader, and (capture being
        # on) the page's native XHR wrapper is off too.
        note += ("hosts matching IE_CAPTURE_BYPASS (" + ", ".join(settings.capture_bypass)
                 + ") bypass the proxy: their requests are not captured, they get no loader "
                 "and their native XHR is not recorded; ")
    return note + ZONE_SENTENCE


def modifications(settings=None):
    """ie_status ``modifications``: the Fidelity-table rows that are active now.

    A row whose switch is off is left out. The per-call opt-in rows (async click, COM input,
    the COM request wrapper) are always listed with their trigger: they cost nothing until a
    call asks for them. Every row ends with how to switch it off.
    """
    settings = config.SETTINGS if settings is None else settings
    # A proxy that could not start, or that the browser did not use (capture_active False),
    # modifies nothing; unverified (None) and verified (True) sessions are modified.
    proxy = (settings.capture and not SESSION.capture_error
             and SESSION.capture_active is not False)
    instrument = settings.instrument
    off_instrument = " (off: IE_INSTRUMENT=0)"
    rows = []
    if proxy and instrument:
        # The proxy only modifies documents when it has a script to insert, so these three
        # rows depend on IE_INSTRUMENT as well (the spec's Fidelity table says so).
        rows += [
            "Loader <script> (absolute src .../__iemcp__/i.js on the document's host; it removes "
            "itself when it runs) inserted by the proxy into HTML documents only: never "
            "fragments, plain text, UTF-16 bodies, HEAD/204/304, head-less framesets or <?xml "
            "documents" + off_instrument,
            "Accept-Encoding: identity on requests whose Accept contains text/html (documents, "
            "and XHRs that ask for HTML)" + off_instrument,
            "Cache-Control: no-store and no validators on the responses the proxy modified "
            "(Back re-fetches the page; Back to a POST result shows 'Webpage has expired')"
            + off_instrument,
        ]
    if proxy and settings.capture_no_cache:
        rows.append("Conditional request headers stripped (pages are re-fetched instead of "
                    "served from the cache) (off: IE_CAPTURE_NO_CACHE=0)")
    if instrument:
        rows += [
            "window.onerror replaced by a recording wrapper that calls the page's own handler "
            "with the same arguments and returns its value" + off_instrument,
            "alert/confirm/prompt replaced by recording wrappers that call the original with "
            "the caller's arguments (typeof alert becomes function where it was object)"
            + off_instrument,
            "showModalDialog replaced by a recording wrapper (arguments and dialogArguments "
            "passed through unchanged)" + off_instrument,
            "window.open replaced by a recording wrapper" + off_instrument,
            "The page's own console methods replaced by recording wrappers that call the "
            "original (only where window.console exists)" + off_instrument,
            "XMLHttpRequest.prototype.open/send replaced by recording wrappers (ActiveX XMLHTTP "
            "is never touched): on https: pages, and on every page while the proxy is off or "
            "saw no traffic" + off_instrument,
            "Window expandos __iemcp, __iemcp_stub and __iemcp_wrap_xhr" + off_instrument,
            "Hook <script> element appended and removed again right after it ran (only where "
            "window.execScript is absent, documentMode 11)" + off_instrument,
        ]
    if proxy:
        rows.append(
            "Proxy in the request path: it may move hosts that normally bypass a proxy into "
            "another IE security zone (Protected Mode, automatic logon, Compatibility View and "
            "with it the document mode) (off: IE_CAPTURE=0, or list those hosts in "
            "IE_CAPTURE_BYPASS)")
    if settings.interaction_mode.name == "background":
        rows.append("IEDriver native input without window focus (window messages: no focus, no "
                    "physical mouse) (off: IE_INTERACTION_MODE=native, which takes focus)")
    if instrument and settings.console_stub:
        rows.append("A complete no-op console created where the page has none (off: "
                    "IE_CONSOLE_STUB=0)")
    rows += [
        "Async (script) clicks: e.click() queued with setTimeout, not user-initiated (no "
        "mousedown/mouseup/focus; the pop-up blocker may block window.open) (only for ie_click "
        f"mode=async; default IE_CLICK_MODE={settings.click_mode}) (off: mode=driver, "
        "IE_CLICK_MODE=driver)",
        "COM input on dialogs and attached tabs: synthetic script events, a timer click and "
        "posted key messages (only with a hwnd:/tab: target) (off: omit target)",
        "COM request wrapper: every COM read or action runs as script and leaves the window "
        "expandos __iemcp_r and __iemcp_e on the target window, cleared after each read-back "
        "(only with a hwnd:/tab: target) (off: omit target)",
    ]
    return rows


def status_info():
    """The part of ie_status that needs no browser call (and is true without a browser)."""
    settings = config.SETTINGS
    state = capture_state(settings)
    info = {
        "active": SESSION.driver is not None,
        "interaction_mode": settings.interaction_mode.name,
        "session_id": settings.session_paths.session_id,
        "session_directory": str(settings.session_paths.directory),
        "driver_pid": SESSION.driver_pid,
        "edge_pid": SESSION.edge_pid,
        "profile": SESSION.profile,
        # Page modifications (R13): wrappers are installed once a hook has run; xhr_wrapped is
        # the flag last sent to a page (or what the next page will get).
        "instrumented": console.is_instrumented(),
        "console_stub": bool(settings.instrument and settings.console_stub),
        "xhr_wrapped": bool(settings.instrument and (
            console.LAST_FLAGS or console.page_flags())["wrap_xhr"]),
        "capture": state,
        "capture_note": capture_note(state, settings),
    }
    if settings.capture and settings.capture_upstream_proxy_error:
        info["capture_config_error"] = settings.capture_upstream_proxy_error
    errors = list(getattr(SESSION.proxy, "internal_errors", None) or [])
    if errors:
        # Unexpected exceptions inside the proxy (bugs): never dropped silently. The text may
        # echo a request, so it goes through the redactor like the log.
        info["capture_internal_errors"] = {
            "count": len(errors), "last": session.REDACTOR.redact_text(errors[-1])}
    info["modifications"] = modifications(settings)
    return info


@tool(
    "ie_status",
    "Session state as JSON: active/alive, title, url, window_handles, and the "
    "interaction mode, session identity/directory, owned browser PIDs/profile, the page "
    "modifications: instrumented (wrappers installed), console_stub, xhr_wrapped, and the "
    "network capture: capture (proxy, page-only, off, unverified), capture_note (what it does "
    "not show), capture_internal_errors (proxy bugs, if any) and modifications (every active "
    "change the harness makes to pages, requests and input, each with its off switch). "
    "Never waits for a busy browser: while another call holds it, title/url/window_handles "
    "are left out and \"driver\": \"busy\" is reported.",
    {"type": "object", "properties": {}},
    needs_driver=False,
    # It waits STATUS_LOCK_WAIT for the lock itself, so a tiny IE_TOOL_TIMEOUT must not make
    # the watchdog answer first.
    budget=lambda args: STATUS_LOCK_WAIT + 5,
)
def t_status(args):
    info = status_info()
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
