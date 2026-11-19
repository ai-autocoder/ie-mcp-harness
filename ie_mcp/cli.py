"""Command line: ``--selftest``, ``--install``, ``--uninstall``, or serve MCP over stdio."""
import json
import os
import subprocess
import sys
import time

from ie_mcp import config
from ie_mcp import protocol
from ie_mcp import tools  # noqa: F401  (registers every tool)
from ie_mcp.protocol import serve
from ie_mcp.session import (SESSION, _same_process, acquire_lock, ensure_ie_policies,
                            kill_verified, log)
from ie_mcp.win32 import capture
from ie_mcp_fixtures import start_fixture_server


PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


class Report:
    """One ``PASS``/``FAIL``/``SKIP`` line per check, then ``RESULT:`` (a FAIL fails the run)."""

    def __init__(self):
        self.failed = False

    def add(self, status, label, detail=""):
        print(f"  {status}  {label}" + (f": {detail}" if detail else ""))
        self.failed = self.failed or status == FAIL
        return status

    def run(self, label, check):
        """Run ``check()``, which returns ``(status, detail)``; an exception is a FAIL."""
        try:
            status, detail = check()
        except Exception as exc:  # noqa: BLE001 - a selftest reports, it never crashes
            status, detail = FAIL, (str(exc).strip().splitlines() or [type(exc).__name__])[0]
        return self.add(status, label, detail)

    def result(self):
        print("RESULT:", "FAIL" if self.failed else "PASS")
        return 1 if self.failed else 0


def session_verdict(user_agent, document_mode):
    """The IE-mode check: a Trident userAgent (not Chromium) and documentMode 7 on form.htm."""
    ua = user_agent or ""
    is_ie = ("Trident" in ua or "MSIE" in ua) and "Chrome/" not in ua
    if is_ie and document_mode == 7:
        return PASS, "Trident userAgent, documentMode 7"
    if not is_ie:
        return FAIL, f"not the IE engine (Chromium?): {ua!r}; check the IE-mode site list"
    return FAIL, f"documentMode is {document_mode!r}, expected 7 (form.htm sets IE=7)"


def proxy_verdict(capture_enabled, state, note):
    if not capture_enabled:
        return SKIP, "IE_CAPTURE=0"
    if state == "proxy":
        return PASS, "the browser's traffic goes through the recording proxy"
    return FAIL, f"capture is {state}: {note}"


def loader_verdict(state, instrument, loader_ran, document_mode):
    """The loader <script> the proxy inserts must run before the page's scripts (proved by the
    load-time error of errors.htm reaching ie_console) and must not change the document mode."""
    if state != "proxy" or not instrument:
        return SKIP, "needs capture=proxy and IE_INSTRUMENT=1"
    if not loader_ran:
        return FAIL, "the load-time error of errors.htm was not recorded: no loader ran before the page's scripts"
    if document_mode == 7:
        return PASS, "the loader ran before the page's scripts; documentMode stays 7"
    return FAIL, f"documentMode is {document_mode!r} through the proxy, expected 7"


def instrumentation_verdict(instrument, marker_type, instrumented):
    if not instrument:
        return SKIP, "IE_INSTRUMENT=0"
    if marker_type == "object" and instrumented:
        return PASS, "window.__iemcp present, ie_status instrumented"
    return FAIL, f"typeof window.__iemcp is {marker_type!r}, ie_status instrumented={instrumented}"


def _tool(name, recorded, **arguments):
    """Call a tool as an MCP client would; its text items. A tool error raises."""
    response = protocol.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": name, "arguments": arguments}})
    for pid, ident in ((SESSION.driver_pid, SESSION.driver_identity),
                       (SESSION.edge_pid, SESSION.edge_identity)):
        if pid and ident and (pid, ident) not in recorded:
            recorded.append((pid, ident))  # every process the session ever had, for the last check
    result = response["result"]
    texts = [i["text"] for i in result["content"] if i.get("type") == "text"]
    if result.get("isError"):
        raise RuntimeError(f"{name}: {texts[0] if texts else 'error'}")
    return texts


def _modal_check(base, recorded, tool=_tool, sleep=time.sleep):
    """Open the opener's lookup dialog, see it in ie_windows, close it.

    A click that errors, or whose dialog shows up late, is not an abort: the dialogs are
    looked at before every click (a late dialog is never clicked over) and after it, and
    the only outcomes are a PASS or one FAIL line saying what was missing.
    """
    def dialogs():
        return [w for w in json.loads(tool("ie_windows", recorded)[0]) if w["kind"] == "html_dialog"]

    tool("ie_goto", recorded, url=base + "modal_opener.htm", wait=1)
    dialog, click_error = None, None
    for _ in range(3):  # native input without focus loses an occasional click (spike D17)
        dialog = next(iter(dialogs()), None)
        if dialog:
            break
        try:
            tool("ie_click", recorded, by="id", value="open_lookup", wait=0)
        except RuntimeError as exc:
            click_error = str(exc).splitlines()[0]  # the dialog may still have opened
        for _ in range(24):  # 6 s
            dialog = next(iter(dialogs()), None)
            if dialog:
                break
            sleep(0.25)
        if dialog:
            break
    if dialog is None:
        raise RuntimeError("no modal dialog showed up in ie_windows after clicking open_lookup"
                           + (f" (last click error: {click_error})" if click_error else ""))
    tool("ie_close_window", recorded, target=dialog["target"])
    for _ in range(40):  # 10 s
        if not dialogs():
            return PASS, f"{dialog.get('title') or 'dialog'!r} detected and closed"
        sleep(0.25)
    raise RuntimeError("the modal dialog is still open after ie_close_window")


def _static_checks(report, settings):
    """Run the dependency checks; ``(can_open_a_browser, has_pywin32)``."""
    def selenium_check():
        import selenium
        return PASS, selenium.__version__

    def path_check(path, hint):
        if path and os.path.exists(path):
            return PASS, path
        return FAIL, f"{path or 'not found'} ({hint})"

    def pywin32_check():
        try:
            import pythoncom  # noqa: F401
            import win32com.client  # noqa: F401
        except ImportError:
            return FAIL, 'not installed: pip install "ie-mcp[win32]" (modal dialogs and tabs need it)'
        return PASS, "pythoncom, win32com"

    required = [
        report.run("selenium import", selenium_check),
        report.run("IEDriverServer", lambda: path_check(settings.driver_path, "set IE_DRIVER_PATH")),
        report.run("Edge", lambda: path_check(settings.edge_path, "set IE_EDGE_PATH")),
    ]
    has_pywin32 = report.run("pywin32", pywin32_check) == PASS
    # The browser checks need selenium, the driver and Edge. Without pywin32 the server still
    # runs, so they still run; only the checks that need COM report it themselves.
    return FAIL not in required, has_pywin32


def _browser_checks(report, settings, recorded, has_pywin32=True):
    fixture = None
    try:
        def start_fixture():
            nonlocal fixture
            fixture = start_fixture_server()
            return PASS, fixture.base_url
        if report.run("fixture server", start_fixture) == FAIL:
            return
        base = fixture.base_url
        ensure_ie_policies()

        def js(script):
            return json.loads(_tool("ie_js", recorded, script=script)[0])

        def session_check():
            # The initial URL is not proxied (spike D11), so this measures the plain page.
            _tool("ie_open", recorded, url=base + "form.htm", wait=1)
            return session_verdict(js("return navigator.userAgent"), js("return document.documentMode"))
        if report.run("IE-mode session", session_check) == FAIL:
            return

        state = []

        def proxy_check():
            if settings.capture:  # the first navigation after creation verifies the proxy
                _tool("ie_goto", recorded, url=base + "form.htm", wait=1)
            status = json.loads(_tool("ie_status", recorded)[0])
            state.append(status["capture"])
            return proxy_verdict(settings.capture, status["capture"], status.get("capture_note", ""))
        report.run("proxy verified", proxy_check)

        def loader_check():
            current = state[0] if state else "off"
            if current != "proxy" or not settings.instrument:
                return loader_verdict(current, settings.instrument, False, None)
            # errors.htm throws in window.onload: only the loader, which runs before the page's
            # own scripts, can have recorded that error (the post-call hook comes too late).
            _tool("ie_goto", recorded, url=base + "errors.htm", wait=1)
            ran = False
            for _ in range(8):
                _tool("ie_text", recorded)  # the post-call hook drains the page's records
                records = json.loads(_tool("ie_console", recorded)[0])["records"]
                ran = any(r["kind"] == "error" and "undefinedFunctionCall" in r["text"]
                          for r in records)
                if ran:
                    break
                time.sleep(0.5)
            _tool("ie_goto", recorded, url=base + "form.htm", wait=1)
            return loader_verdict(current, settings.instrument, ran, js("return document.documentMode"))
        report.run("loader kept documentMode", loader_check)

        def instrumentation_check():
            marker = js("return typeof window.__iemcp")
            status = json.loads(_tool("ie_status", recorded)[0])
            return instrumentation_verdict(settings.instrument, marker, status["instrumented"])
        report.run("instrumentation present", instrumentation_check)

        if has_pywin32:
            report.run("modal dialog", lambda: _modal_check(base, recorded))
        else:
            report.add(SKIP, "modal dialog", "needs pywin32 (see the pywin32 line above)")
    finally:
        try:
            _tool("ie_close", recorded)
        except Exception:  # noqa: BLE001 - SESSION.close() below is the fallback
            pass
        SESSION.close()
        if fixture is not None:
            fixture.stop()


def _leftover_check(recorded):
    alive = [(pid, ident) for pid, ident in recorded if _same_process(pid, ident)]
    if not alive:
        return PASS, f"{len(recorded)} process(es) started, none left"
    kill_verified(alive)  # only by recorded identity: never a process that is not ours
    return FAIL, f"still running after close (now killed): {[pid for pid, _ in alive]}"


def selftest():
    """Diagnose the setup without an MCP client: dependencies and paths, then a real IE-mode
    session against the bundled fixture site (document mode, proxy, instrumentation, a modal
    dialog), then that no process is left. One line per check; exit 0 only if none failed."""
    settings = config.SETTINGS
    report = Report()
    recorded = []
    print("ie-mcp selftest")
    print(f"  interaction_mode={settings.interaction_mode.name} capture={settings.capture} "
          f"instrument={settings.instrument} clean_session={settings.clean_session}")
    can_browse, has_pywin32 = _static_checks(report, settings)
    if can_browse:
        _browser_checks(report, settings, recorded, has_pywin32)
        report.run("no leftover processes", lambda: _leftover_check(recorded))
    else:
        report.add(SKIP, "browser checks", "needs selenium, the driver and Edge: fix the failed checks above first")
    return report.result()


def _self_command():
    """The command an MCP client should run to launch this server.

    Prefer the installed `ie-mcp` console script (no python, no path); fall back
    to `python <abspath of ie_mcp/__main__.py>` when running straight from a clone.
    """
    import shutil
    exe = shutil.which("ie-mcp")
    if exe:
        return [exe]
    return [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "__main__.py")]


def install(remove=False):
    """Register (or with remove=True, unregister) ie-mcp with every MCP client
    found on PATH. The launch path is resolved automatically — nothing to type."""
    import shutil
    cmd = _self_command()
    verb = "remove" if remove else "add"
    clients = {
        "claude": ["claude", "mcp", "remove", "ie-mcp"] if remove
                  else ["claude", "mcp", "add", "ie-mcp", "--", *cmd],
        "codex":  ["codex", "mcp", "remove", "ie-mcp"] if remove
                  else ["codex", "mcp", "add", "ie-mcp", "--", *cmd],
        # gemini's `mcp add` takes the command as positional args (no `--`).
        "gemini": ["gemini", "mcp", "remove", "ie-mcp"] if remove
                  else ["gemini", "mcp", "add", "ie-mcp", *cmd],
    }
    print(f"ie-mcp {verb} (launch: {' '.join(cmd)})")
    done = False
    for name, argv in clients.items():
        if not shutil.which(name):
            print(f"  {name}: not on PATH, skipping")
            continue
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=30)
            out = (r.stderr or r.stdout or "").strip().splitlines()
            print(f"  {name}: {'OK' if r.returncode == 0 else 'FAIL'}"
                  + (f" — {out[0]}" if out else ""))
            done = done or r.returncode == 0
        except Exception as exc:
            print(f"  {name}: ERROR {exc}")
    if not done and not remove:
        print("  (no clients updated — install claude and/or codex CLI first)")
    return 0 if (done or remove) else 1



def main():
    # Before any window call (modal probe, enumeration, capture): GetWindowRect and PrintWindow
    # only agree on a scaled display once the process is DPI aware, and it cannot be set later.
    capture.enable_dpi_awareness()
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if "--install" in sys.argv:
        sys.exit(install())
    if "--uninstall" in sys.argv:
        sys.exit(install(remove=True))
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    except Exception:
        pass
    ensure_ie_policies()
    acquire_lock()
    paths = config.SETTINGS.session_paths
    # Not "session=": that is a secret field name, so the log would redact (and then mask) the id.
    log(f"ready id={paths.session_id} directory={paths.directory}")
    try:
        serve(sys.stdin, sys.stdout)
    finally:
        SESSION.close()  # also releases the lease


if __name__ == "__main__":
    main()
