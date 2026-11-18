"""Command line: ``--selftest``, ``--install``, ``--uninstall``, or serve MCP over stdio."""
import os
import subprocess
import sys
import time

from ie_mcp import config
from ie_mcp import tools  # noqa: F401  (registers every tool)
from ie_mcp.protocol import serve
from ie_mcp.session import SESSION, acquire_lock, ensure_ie_policies, log


def selftest():
    """Diagnose the setup without an MCP client: checks deps/paths, then opens and
    closes a real IE-mode session against IE_DEFAULT_URL. Prints a report; exit 0/1."""
    settings = config.SETTINGS
    ok = True
    print("ie-mcp selftest")
    try:
        import selenium
        print(f"  selenium: {selenium.__version__}")
    except Exception as exc:
        print(f"  selenium: FAIL ({exc})"); ok = False
    for label, path in [("edge", settings.edge_path), ("iedriver", settings.driver_path)]:
        exists = bool(path) and os.path.exists(path)
        shown = path or "(not found; set IE_DRIVER_PATH)"
        print(f"  {label}: {'OK' if exists else 'MISSING'} {shown}")
        ok = ok and exists
    print(f"  interaction_mode: {settings.interaction_mode.name}")
    print(f"  clean_session: {'ON (clears the IE-mode cookie store and cache)' if settings.clean_session else 'off'}")
    print(f"  site_list: {settings.site_list or '(none)'}")
    print(f"  default_url: {settings.default_url}")
    print("  opening a session ...")
    try:
        ensure_ie_policies()
        d = SESSION.ensure(initial_url=settings.default_url)
        time.sleep(3)
        print(f"  session OK: title={d.title!r} url={d.current_url}")
        ua = d.execute_script("return navigator.userAgent;")
        is_ie = ("Trident" in ua or "MSIE" in ua) and "Chrome/" not in ua
        print(f"  userAgent: {ua}")
        print(f"  IE engine: {'YES' if is_ie else 'NO (Chromium?)'}")
        ok = ok and is_ie
    except Exception as exc:
        print(f"  session: FAIL ({exc})"); ok = False
    finally:
        SESSION.close()
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


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
    log(f"ready session={paths.session_id} directory={paths.directory}")
    try:
        serve(sys.stdin, sys.stdout)
    finally:
        SESSION.close()  # also releases the lease


if __name__ == "__main__":
    main()
