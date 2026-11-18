"""Session ownership, process tracking and the long-lived IEDriver session.

One MCP process owns one browser. Settings are read from ``config.SETTINGS`` at call
time, never copied into module globals.
"""
import csv
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from ie_mcp import config
from ie_mcp.win32.processes import process_identity
# Re-exported: these are part of this module's public surface (and of upstream's).
from ie_mcp.config import SessionPaths, build_session_paths  # noqa: F401


def log(*a):
    msg = "[ie-mcp] " + " ".join(str(x) for x in a)
    print(msg, file=sys.stderr, flush=True)
    log_file = config.SETTINGS.session_paths.log_file
    if log_file:
        try:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, "a", encoding="utf-8") as fh:
                fh.write(msg + "\n")
        except Exception:
            pass


# Serialises every tool that talks to the IEDriver (it is not thread-safe, and frame state is
# per session). A plain Lock, not an RLock: a tool call's POST_HOOKS sub-thread releases it,
# not the thread that acquired it. protocol.run_tool owns the acquire/release protocol.
DRIVER_LOCK = threading.Lock()


class Cancelled(Exception):
    """A running wait or find was stopped by ``ie_close(force=true)`` or by ``close()``."""


# --- per-agent ownership (one MCP process owns one browser) ------------------
def pid_alive(pid, run=subprocess.run):
    """True if a process with exactly this PID exists.

    Quirk: ``tasklist`` output is a table; a substring test finds PID 12 inside the row of
    PID 123 (or in a memory column). The CSV output is parsed and the PID column compared.
    """
    try:
        pid = int(pid)
        out = run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                  capture_output=True, text=True, timeout=10).stdout
    except Exception:
        # tasklist failed or timed out. Calling a live process "dead" would let a second
        # server take over its session, so fall back to a direct query before giving up.
        try:
            return process_identity(pid) is not None
        except Exception:
            return False
    for row in csv.reader(out.splitlines()):
        if len(row) >= 2 and row[1].strip() == str(pid):
            return True
    return False


_PID_ALIVE = pid_alive  # SessionLease and cleanup_stale_sessions take a parameter of that name


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            value = json.load(fh)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json_atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(temporary, "w", encoding="utf-8") as fh:
        json.dump(value, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(temporary, path)


class SessionLease:
    """Atomic lease for one logical agent session; different IDs never contend."""

    def __init__(self, paths, pid=None, pid_alive=None):
        self.paths = paths
        self.pid = os.getpid() if pid is None else int(pid)
        self.pid_alive = pid_alive or _PID_ALIVE
        self.acquired = False

    def _record(self, **updates):
        record = _read_json(self.paths.owner_file)
        record.update({
            "session_id": self.paths.session_id,
            "owner_pid": self.pid,
            "updated_at": time.time(),
            **updates,
        })
        _write_json_atomic(self.paths.owner_file, record)
        return record

    def _preserve_stale_owner(self):
        """Rename a dead holder's ``owner.json`` to ``owner.stale-<pid>.json``.

        The record names the browser that holder left behind (PIDs, profile, identities).
        Taking the lease over must not erase it, or ``cleanup_stale_sessions`` could never
        find the orphans; it globs ``owner*.json``.
        """
        owner_file = self.paths.owner_file
        if not owner_file.exists():
            return
        holder = _read_json(owner_file).get("owner_pid")
        if holder == self.pid:
            return
        label = holder if isinstance(holder, int) else "unknown"
        target = owner_file.with_name(f"owner.stale-{label}.json")
        counter = 1
        while target.exists():  # the same PID can die twice (PID reuse); keep every record
            counter += 1
            target = owner_file.with_name(f"owner.stale-{label}-{counter}.json")
        try:
            os.replace(owner_file, target)
        except OSError as exc:
            log(f"could not preserve stale owner record {owner_file}: {exc}")

    def acquire(self):
        if self.acquired:
            return
        self.paths.directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "session_id": self.paths.session_id,
            "owner_pid": self.pid,
            "created_at": time.time(),
        }
        for _ in range(3):
            try:
                fd = os.open(str(self.paths.lock_file), os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            except FileExistsError:
                holder = _read_json(self.paths.lock_file)
                holder_pid = holder.get("owner_pid")
                if holder_pid == self.pid:
                    self.acquired = True
                    self._record()
                    return
                if isinstance(holder_pid, int) and self.pid_alive(holder_pid):
                    raise RuntimeError(
                        f"IE session '{self.paths.session_id}' is already owned by "
                        f"ie-mcp process PID {holder_pid}. Use a different IE_SESSION_ID."
                    )
                # Dead holder: keep its record for ie_kill_orphans, then take the lock.
                self._preserve_stale_owner()
                try:
                    self.paths.lock_file.unlink()
                except FileNotFoundError:
                    pass
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, sort_keys=True)
                fh.write("\n")
            self.acquired = True
            # A record left without its lock (the holder died between deleting the two).
            self._preserve_stale_owner()
            self._record(driver_pid=None, edge_pid=None, profile=None,
                         driver_identity=None, edge_identity=None)
            return
        raise RuntimeError(f"could not acquire IE session lease '{self.paths.session_id}'")

    def update(self, **updates):
        return self._record(**updates)

    def release(self):
        lock = _read_json(self.paths.lock_file)
        if lock.get("owner_pid") == self.pid and lock.get("session_id") == self.paths.session_id:
            try:
                self.paths.lock_file.unlink()
            except FileNotFoundError:
                pass
        owner = _read_json(self.paths.owner_file)
        if owner.get("owner_pid") == self.pid and owner.get("session_id") == self.paths.session_id:
            try:
                self.paths.owner_file.unlink()
            except FileNotFoundError:
                pass
        self.acquired = False


LEASE = SessionLease(config.SETTINGS.session_paths)


def acquire_lock():
    if config.SETTINGS.use_lock:
        LEASE.acquire()


def release_lock():
    if config.SETTINGS.use_lock:
        LEASE.release()


# --- ensure Edge IE-mode policies are set (self-healing, HKCU = no admin) -----
def ensure_ie_policies():
    site_list = config.SETTINGS.site_list
    if not site_list:
        log("IE_SITE_LIST not set; not managing Edge IE-mode policy")
        return
    try:
        import winreg
    except ImportError:
        return
    key_path = r"SOFTWARE\Policies\Microsoft\Edge"
    site_url = "file:///" + os.path.abspath(site_list).replace("\\", "/")
    wanted = {
        "InternetExplorerIntegrationLevel": (winreg.REG_DWORD, 1),
        "InternetExplorerIntegrationSiteList": (winreg.REG_SZ, site_url),
        "InternetExplorerIntegrationReloadInIEModeAllowed": (winreg.REG_DWORD, 1),
    }
    # 1) read-only check: if policies are already correct, do nothing.
    current = {}
    try:
        rk = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ)
        for name in wanted:
            try:
                current[name], _ = winreg.QueryValueEx(rk, name)
            except FileNotFoundError:
                current[name] = None
        winreg.CloseKey(rk)
    except FileNotFoundError:
        current = {name: None for name in wanted}
    except OSError:
        current = None

    if current is not None and all(current.get(n) == v for n, (t, v) in wanted.items()):
        log("IE-mode policies OK")
        return

    # 2) something is missing/wrong -> try to write (may need elevation).
    try:
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path)
        for name, (typ, val) in wanted.items():
            winreg.SetValueEx(key, name, 0, typ, val)
            log(f"policy set {name} = {val}")
        winreg.CloseKey(key)
    except OSError as exc:
        log(f"WARN IE-mode policy needs updating but the key is not writable "
            f"({exc}). Run once elevated, or set HKCU\\{key_path} manually: "
            f"InternetExplorerIntegrationLevel=1 (DWORD), "
            f"InternetExplorerIntegrationSiteList={site_url}")


# --- process tracking (which Edge IE-mode windows are open) ------------------
def list_ie_browsers():
    """Enumerate all Edge IE-mode windows + IEDriverServer processes system-wide.

    Returns a list of dicts: {pid, kind, profile, url, parent}. Identifies IE-mode Edge by
    the --ie-mode-force flag and pulls the per-session IEDriver-<uuid> profile and
    the launch URL from the command line. ``parent`` is the parent PID (or None): the
    IE-mode Edge that IEDriver starts is its child, which is how ownership is proven.
    Uses PowerShell/CIM (Windows-only).
    """
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' or Name='IEDriverServer.exe'\" |"
        " ForEach-Object { $_.Name + '|' + $_.ProcessId + '|' + $_.ParentProcessId + '|' +"
        " ($_.CommandLine -replace '[\\r\\n]',' ') }"
    )
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=20).stdout
    except Exception as exc:
        log(f"list_ie_browsers failed: {exc}")
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split("|", 3)
        if len(parts) < 2:
            continue
        name, pid = parts[0].strip(), parts[1].strip()
        if not pid.isdigit():
            continue
        pid = int(pid)
        parent, cmd = None, ""
        if len(parts) > 2:
            # Defensive: a line without the parent column (older output) is all command line.
            if parts[2].strip().isdigit():
                parent = int(parts[2].strip())
                cmd = parts[3] if len(parts) > 3 else ""
            else:
                cmd = "|".join(parts[2:])
        if name.lower() == "iedriverserver.exe":
            rows.append({"pid": pid, "kind": "iedriver", "profile": None, "url": None,
                         "parent": parent})
            continue
        # only the main IE-mode browser process (skip helper --type=... procs)
        if "--ie-mode-force" not in cmd or "--type=" in cmd:
            continue
        prof = re.search(r"IEDriver-([0-9a-fA-F-]+)", cmd)
        url = re.search(r"(https?://\S+)", cmd)
        rows.append({"pid": pid, "kind": "edge-ie",
                     "profile": prof.group(1) if prof else None,
                     "url": url.group(1) if url else None,
                     "parent": parent})
    return rows


def _as_identity(value):
    """``(str, int)`` for a well-formed identity, else None (unknown, never matches).

    owner.json is plain data that anything can edit or corrupt; a malformed identity must
    mean "unknown", never an exception out of a kill path.
    """
    if isinstance(value, (list, tuple)) and len(value) == 2:
        name, created = value
        if isinstance(name, str) and isinstance(created, int) and not isinstance(created, bool):
            return name, created
    return None


def _same_process(pid, recorded, identity=None):
    """True if ``pid`` is alive with exactly the recorded (well-formed) identity."""
    recorded = _as_identity(recorded)
    if recorded is None:
        return False
    try:
        return _as_identity((identity or process_identity)(pid)) == recorded
    except Exception:
        return False


def kill_verified(targets, identity=process_identity, run=subprocess.run):
    """Kill the processes in ``targets`` that are still the ones that were recorded.

    ``targets`` is a list of ``(pid, recorded_identity)``. A process is killed only when
    ``identity(pid)`` equals the recorded ``(image name, creation time)``: Windows reuses
    PIDs, so a PID alone may now be somebody else's process. A recorded identity of ``None``
    never matches (an unknown process is never killed). JSON gives identities back as lists,
    so both sides are compared as tuples. There is no ``/T``: Edge's child processes exit
    with their browser process, and a tree kill is how a wrong PID takes down more.
    Returns the PIDs killed.
    """
    killed = []
    for pid, recorded in targets:
        if _as_identity(recorded) is None:
            log(f"not killing {pid}: no (valid) recorded identity")
            continue
        if not _same_process(pid, recorded, identity):
            continue
        try:
            result = run(["taskkill", "/F", "/PID", str(int(pid))],
                         capture_output=True, text=True, timeout=15)
        except Exception as exc:
            log(f"kill {pid} failed: {exc}")
            continue
        code = getattr(result, "returncode", 0)
        if isinstance(code, int) and code != 0:
            log(f"taskkill {pid} exited with {code}")
            continue
        killed.append(pid)
    return killed


def _owned_processes_for_record(record, browser_by_pid, identity=process_identity):
    """``(pid, recorded_identity)`` pairs that still match a stale record strongly enough.

    Two independent checks, both required: the Edge profile is still the one the record
    names (upstream rule), and each process still has the recorded identity (image name and
    creation time). A record without identities (written by an older version) kills nothing.
    """
    profile = record.get("profile")
    edge_pid = record.get("edge_pid")
    driver_pid = record.get("driver_pid")
    edge = browser_by_pid.get(edge_pid)
    driver = browser_by_pid.get(driver_pid)

    same_process = lambda pid, recorded: _same_process(pid, recorded, identity)  # noqa: E731

    edge_matches = bool(
        edge
        and edge.get("kind") == "edge-ie"
        and profile
        and edge.get("profile") == profile
        and same_process(edge_pid, record.get("edge_identity"))
    )
    owned = []
    # The Edge profile is the durable identity for an IEDriver-created browser.
    # Only trust the associated driver PID when that profile still exists too;
    # this prevents PID reuse from killing a newer agent's unrelated driver.
    if (edge_matches and driver and driver.get("kind") == "iedriver"
            and same_process(driver_pid, record.get("driver_identity"))):
        owned.append((driver_pid, _as_identity(record["driver_identity"])))
    if edge_matches:
        owned.append((edge_pid, _as_identity(record["edge_identity"])))
    return owned


def cleanup_stale_sessions(session_root, browser_rows=None, pid_alive=None,
                           identity=process_identity, kill=None):
    """Kill only browsers registered to dead ie-mcp owner processes.

    Unknown browser processes and sessions whose owner PID is alive are always
    preserved. Session records remain on disk with cleared process identities so
    their scoped logs are retained for diagnosis. Records are read from ``owner*.json``,
    which includes the ``owner.stale-<pid>.json`` files a lease takeover leaves behind.
    ``kill`` receives ``[(pid, recorded_identity), ...]`` and defaults to ``kill_verified``.
    """
    session_root = Path(session_root)
    pid_alive = pid_alive or _PID_ALIVE
    if kill is None:
        kill = lambda targets: kill_verified(targets, identity=identity)  # noqa: E731
    browser_rows = list_ie_browsers() if browser_rows is None else list(browser_rows)
    browser_by_pid = {row.get("pid"): row for row in browser_rows if row.get("pid")}
    cleanup_targets = []
    stale_records = []
    skipped_live_sessions = set()

    if session_root.exists():
        for owner_file in sorted(session_root.glob("*/owner*.json")):
            record = _read_json(owner_file)
            session_id = record.get("session_id") or owner_file.parent.name
            owner_pid = record.get("owner_pid")
            if isinstance(owner_pid, int) and pid_alive(owner_pid):
                skipped_live_sessions.add(session_id)
                continue
            owned = _owned_processes_for_record(record, browser_by_pid, identity)
            if owned:
                known = {pid for pid, _ in cleanup_targets}
                cleanup_targets.extend(t for t in owned if t[0] not in known)
                stale_records.append((owner_file, record))

    killed = list(kill(cleanup_targets)) if cleanup_targets else []
    for owner_file, record in stale_records:
        record.update({
            "driver_pid": None,
            "edge_pid": None,
            "profile": None,
            "driver_identity": None,
            "edge_identity": None,
            "cleaned_at": time.time(),
        })
        _write_json_atomic(owner_file, record)

    cleanup_set = {pid for pid, _ in cleanup_targets}
    return {
        "killed": killed,
        "skipped_live_sessions": sorted(skipped_live_sessions),
        "untracked_process_pids": sorted(
            row["pid"] for row in browser_rows if row.get("pid") not in cleanup_set
        ),
    }


# --- IEDriver options, environment and HTTP timeout ---------------------------
# Seconds the Selenium HTTP client waits for IEDriver to answer one command. Without a
# finite value a dead driver hangs the calling thread (selenium's own default depends on
# the version). Long waits (ie_wait_ready) poll in short commands, so 300 is ample.
DRIVER_HTTP_TIMEOUT = 300


def build_options(settings, initial_url, proxy_port=None, bypass=None):
    """The IeOptions for a new session. Pure: no process is started."""
    from selenium import webdriver

    options = webdriver.IeOptions()
    options.attach_to_edge_chrome = True
    options.edge_executable_path = settings.edge_path
    options.ignore_zoom_level = True
    options.ignore_protected_mode_settings = True
    options.require_window_focus = settings.interaction_mode.require_window_focus
    # ensure_clean_session clears the user's whole IE-mode cookie store and cache, not just
    # this session's, so it is opt-in (IE_CLEAN_SESSION=1). Upstream forced it on.
    options.ensure_clean_session = settings.clean_session
    options.native_events = settings.interaction_mode.native_events
    # IEDriver's default ("dismiss and notify") silently cancels an open alert/confirm on any
    # driver command; "ignore" leaves script dialogs alone for the dialog tools.
    options.unhandled_prompt_behavior = "ignore"
    options.browser_attach_timeout = 30_000
    options.page_load_strategy = "none"
    # CRITICAL for IE mode: navigate during session creation so IEDriver
    # crosses the Protected-Mode boundary and reconnects to the new browser
    # process *before* handing back control. Navigating afterwards makes the
    # driver lose (and exit on) the browser.
    if initial_url:
        options.initial_browser_url = initial_url
    if proxy_port is not None:
        from selenium.webdriver.common.proxy import Proxy

        address = f"127.0.0.1:{int(proxy_port)}"
        proxy = Proxy()
        proxy.http_proxy = address
        proxy.ssl_proxy = address
        # Quirk: IEDriver rejects a string here (selenium's Proxy converts strings, a raw
        # capability dict does not), so noProxy is a list. "<-loopback>" makes loopback
        # addresses go through the proxy too (IE bypasses it for them otherwise).
        proxy.no_proxy = ["<-loopback>"] + list(bypass or [])
        options.proxy = proxy
        # Quirk: without this IEDriver writes the user's global WinINet proxy settings.
        options.use_per_process_proxy = True
    return options


def driver_environment(environ=None):
    """A copy of the environment for IEDriverServer, without ``__COMPAT_LAYER``.

    Quirk (spike D16): the Claude desktop app sets ``__COMPAT_LAYER`` (for example
    ``DetectorsAppHealth``). Inherited, it makes Edge relaunch itself, so IEDriver never
    finds the IE window it started. The key is dropped case-insensitively (Windows
    environment names are case-insensitive). ``ie.ignoreprocessmatch`` is not a fix: it
    hides the symptom. Never mutates ``os.environ``.
    """
    source = os.environ if environ is None else environ
    return {k: v for k, v in source.items() if k.upper() != "__COMPAT_LAYER"}


def apply_driver_timeout(driver, seconds=DRIVER_HTTP_TIMEOUT):
    """Set the HTTP client timeout of a created driver.

    Checked against selenium 4.52.0 (the installed version): ``webdriver.Ie`` builds its own
    ``ClientConfig(timeout=120)`` and takes no ``client_config`` argument, so the timeout is
    raised on the live connection through the public ``command_executor.client_config``.
    ``RemoteConnection.set_timeout`` is deprecated there and class-wide. Selenium releases
    before ClientConfig (we support >= 4.14) only have ``set_timeout``, used as the fallback.
    Session creation itself runs under the 120 s default, which is already finite.
    """
    executor = driver.command_executor
    client_config = getattr(executor, "client_config", None)
    if client_config is not None:
        client_config.timeout = seconds
    else:
        type(executor).set_timeout(seconds)


# --- the IE driver wrapper ---------------------------------------------------
# Seconds to wait for webdriver.Ie() to return before the attempt is abandoned. Quirk (spike):
# creation sometimes hangs inside IEDriver after Edge has started, until selenium's own 120 s
# read timeout. IEDriver's browser_attach_timeout is 30 s, so 60 s is that plus a margin.
SESSION_CREATE_TIMEOUT = 60
# Seconds to wait for driver.quit() before the browser is killed instead.
QUIT_TIMEOUT = 10
# Seconds between two failed attempts to create a session.
RETRY_DELAY = 2


def _edge_profiles(rows=None):
    """The IEDriver temp profiles of every IE-mode Edge window currently open."""
    rows = list_ie_browsers() if rows is None else rows
    return {b["profile"] for b in rows if b["kind"] == "edge-ie"}


def _quiet_quit(driver):
    try:
        driver.quit()
    except Exception:
        pass


def _own_new_edge(rows, before_profiles, driver_pid):
    """The one IE-mode Edge row that is provably started by our IEDriver, or None.

    Adopting (and later killing) a window needs more than "it is new". Required: kind
    ``edge-ie``, a truthy IEDriver profile that did not exist before this attempt, AND a
    parent PID equal to our IEDriver's PID. Without the parent test a hand-started
    ``--ie-mode-force`` Edge (no IEDriver profile), another agent's session starting at the
    same moment, or a recycled PID would all pass for ours. Exactly one match is adopted;
    none or several adopts nothing (and is logged): not owning a window only leaks a
    window, owning the wrong one kills somebody's work.
    """
    if not driver_pid or before_profiles is None:
        return None
    matches = [b for b in rows
               if b.get("kind") == "edge-ie" and b.get("profile")
               and b["profile"] not in before_profiles and b.get("parent") == driver_pid]
    if len(matches) == 1:
        return matches[0]
    log(f"{len(matches)} IE-mode windows are new children of IEDriver {driver_pid}; "
        "not adopting any")
    return None


class IeSession:
    def __init__(self):
        self.driver = None
        self.driver_pid = None    # IEDriverServer.exe PID
        self.edge_pid = None      # the IE-mode msedge.exe window PID we own
        # Top-level hwnd of the session's first IE window (what ``windows.classify`` calls
        # "main"). None when unknown; reset whenever the browser is torn down.
        self.main_hwnd = None
        self.profile = None       # IEDriver-<uuid> temp profile that identifies us
        # (image name, creation time) of the two processes above. A PID alone proves nothing
        # once Windows reuses it, so every kill compares these first.
        self.driver_identity = None
        self.edge_identity = None
        # Set by close() and ie_close(force=true): wait loops stop at their next poll.
        self.cancel = threading.Event()
        # The Service of a session being created right now: its IEDriver exists before
        # webdriver.Ie() returns, so a force-close during creation can still find it.
        self._creating_service = None

    # -- cancellation --
    def check_cancel(self):
        if self.cancel.is_set():
            raise Cancelled("cancelled")

    def pause(self, seconds):
        """Sleep ``seconds`` but wake at once (and raise ``Cancelled``) when cancelled."""
        if self.cancel.wait(max(0, seconds)):
            raise Cancelled("cancelled")

    # -- creation --
    def _create_driver(self, webdriver, service, options):
        """``webdriver.Ie(...)`` bounded by ``SESSION_CREATE_TIMEOUT``.

        The call runs in a daemon thread because it cannot be interrupted (see the constant).
        A result that arrives after the attempt was abandoned is quit and discarded.
        """
        box = {}
        lock = threading.Lock()
        state = {"abandoned": False}

        def create():
            try:
                driver = webdriver.Ie(service=service, options=options)
            except BaseException as exc:  # noqa: BLE001 - re-raised in the caller's thread
                with lock:
                    box["error"] = exc
                return
            with lock:
                if not state["abandoned"]:
                    box["driver"] = driver
                    return
            _quiet_quit(driver)

        thread = threading.Thread(target=create, name="ie-create-session", daemon=True)
        thread.start()
        thread.join(SESSION_CREATE_TIMEOUT)
        with lock:
            if "driver" in box:
                return box["driver"]
            if "error" in box:
                raise box["error"]
            state["abandoned"] = True
        raise RuntimeError(f"IE-mode session creation timed out after {SESSION_CREATE_TIMEOUT}s")

    def _reap_failed_attempt(self, service, before_profiles):
        """Kill the IEDriver and the IE-mode Edge a failed attempt left behind.

        Only our own: the driver is the Popen child of ``service``; the Edge is the single
        new IE-mode window whose parent is that driver (``_own_new_edge``). Both go through
        ``kill_verified`` with identities read now.
        """
        targets = []
        driver_pid = getattr(getattr(service, "process", None), "pid", None)
        if driver_pid:
            targets.append((driver_pid, process_identity(driver_pid)))
        if before_profiles is not None:  # unknown "before": no way to tell which Edge is new
            try:
                edge = _own_new_edge(list_ie_browsers(), before_profiles, driver_pid)
            except Exception:
                edge = None
            if edge:
                targets.append((edge["pid"], process_identity(edge["pid"])))
        if targets:
            kill_verified(targets)

    def _build(self, initial_url, settings=None, before_profiles=None):
        from selenium import webdriver
        from selenium.webdriver.ie.service import Service

        settings = config.SETTINGS if settings is None else settings
        driver_path = settings.driver_path
        if not driver_path:
            raise FileNotFoundError(
                "IEDriverServer.exe not found in the per-user Selenium cache "
                "(~/.cache/selenium/IEDriverServer). Set IE_DRIVER_PATH to its full path.")
        if not os.path.isfile(driver_path):
            raise FileNotFoundError(f"IE_DRIVER_PATH does not point to a file: {driver_path}")
        options = build_options(settings, initial_url)
        service = Service(executable_path=driver_path, env=driver_environment())
        driver = None
        self._creating_service = service
        try:
            driver = self._create_driver(webdriver, service, options)
            apply_driver_timeout(driver, DRIVER_HTTP_TIMEOUT)
        except BaseException:
            self._quit_driver(driver, QUIT_TIMEOUT)  # bounded, like every other quit
            self._reap_failed_attempt(service, before_profiles)
            raise
        finally:
            self._creating_service = None
        return driver

    def ensure(self, initial_url=None):
        """Return a live driver, (re)creating it with retries if needed.

        When a new session must be created, initial_url is opened as part of
        session creation (the only reliable way into IE mode). The lease is taken once and
        held across every retry; only ``close()`` releases it.
        """
        from selenium.common.exceptions import WebDriverException

        if self.driver is not None:
            try:
                _ = self.driver.current_url  # cheap liveness probe
                return self.driver
            except WebDriverException:
                log("session dead, recreating")
                self.quit_browser()

        acquire_lock()  # refuse only if another process owns this session identity
        self.cancel.clear()  # a force-close of the previous browser must not stop this one
        retries = config.SETTINGS.attach_retries
        last = None
        for attempt in range(1, retries + 1):
            try:
                log(f"creating IE-mode session (attempt {attempt}/{retries}) url={initial_url}")
                before = _edge_profiles()
                before_tops = self._ie_tops()
                self.driver = self._build(initial_url, before_profiles=before)
                # Record the identities first: if a later step fails, the teardown below
                # can still kill exactly this browser.
                self._capture_identity(before)
                self._record_main_hwnd(before_tops)
                # Force-closed while the browser was being created: do not hand it out. The
                # except block below kills it (its identities were just recorded).
                self.check_cancel()
                self.driver.set_page_load_timeout(config.SETTINGS.page_timeout)
                _ = self.driver.current_url  # verify it survived the boundary crossing
                return self.driver
            except Exception as exc:
                last = exc
                log(f"attach failed: {exc}")
                self.quit_browser()  # keeps the lease
                self.check_cancel()  # a force-close ends the retries too
                if attempt < retries:
                    self.pause(RETRY_DELAY)
        raise RuntimeError(f"could not start Edge IE mode after {retries} tries: {last}")

    def _ie_tops(self, edge_tree_of=None):
        """{hwnd: title} of the top windows with an IE server window inside, or None.

        Only Edge's processes are scanned (``edge_tree_of``: only that process and its
        children). It never raises: a failure here must not break session creation, it only
        leaves ``main_hwnd`` unknown. The process snapshot is read fresh because a before/after
        pair must not share the 2 s cache.
        """
        try:
            from ie_mcp.win32 import processes, windows
            rows = processes.snapshot(fresh=True)
            if edge_tree_of:
                pids = processes.process_tree(edge_tree_of, rows)
            else:
                pids = {r.pid for r in rows if r.exe.lower() == "msedge.exe"}
            return windows.ie_top_windows(pids)
        except Exception as exc:
            log(f"could not list IE windows: {exc}")
            return None

    def _record_main_hwnd(self, before_tops):
        """Set ``main_hwnd`` to the IE window that appeared while the driver started."""
        self.main_hwnd = None
        if before_tops is None:
            return
        if not self.edge_pid:
            # Edge adoption failed. Without our own process tree the scan would cover every
            # Edge window, and a window the user opened meanwhile could be taken for ours.
            log("session main window unknown: our Edge process was not identified")
            return
        try:
            from ie_mcp.win32 import windows
            after = self._ie_tops(self.edge_pid)
            if after is not None:
                self.main_hwnd = windows.pick_main_hwnd(
                    set(before_tops), after, lambda: self.driver.title)
        except Exception as exc:
            log(f"could not tell the main IE window: {exc}")
        log(f"session main window: hwnd={self.main_hwnd}")

    def _capture_identity(self, before_profiles):
        """Record which browser we just opened (driver pid + the new IE-mode edge)."""
        try:
            self.driver_pid = self.driver.service.process.pid
        except Exception:
            self.driver_pid = None
        self.driver_identity = process_identity(self.driver_pid) if self.driver_pid else None
        self.edge_pid = self.profile = self.edge_identity = None
        try:
            edge = _own_new_edge(list_ie_browsers(), before_profiles, self.driver_pid)
        except Exception:
            edge = None
        if edge:
            self.edge_pid, self.profile = edge["pid"], edge["profile"]
            self.edge_identity = process_identity(self.edge_pid)
        LEASE.update(driver_pid=self.driver_pid, edge_pid=self.edge_pid, profile=self.profile,
                     driver_identity=self.driver_identity, edge_identity=self.edge_identity)
        log(f"session identity: driver_pid={self.driver_pid} edge_pid={self.edge_pid} profile={self.profile}")

    # -- teardown --
    def _quit_driver(self, driver, timeout_s):
        """``driver.quit()`` in a daemon thread: a dead driver would block it for minutes."""
        if driver is None:
            return
        thread = threading.Thread(target=_quiet_quit, args=(driver,), name="ie-quit", daemon=True)
        thread.start()
        thread.join(timeout_s)
        if thread.is_alive():
            log(f"driver.quit() still running after {timeout_s}s; killing the browser")

    def kill_own_processes(self):
        """Kill this session's IEDriver and Edge, each only if its identity still matches.

        Includes the IEDriver of a session still being created (its Popen is ours, so the
        PID cannot have been reused). Returns the PIDs killed.
        """
        targets = [(pid, identity) for pid, identity in
                   ((self.driver_pid, self.driver_identity), (self.edge_pid, self.edge_identity))
                   if pid]
        creating = getattr(getattr(self._creating_service, "process", None), "pid", None)
        if creating and creating != self.driver_pid:
            targets.append((creating, process_identity(creating)))
        return kill_verified(targets) if targets else []

    def _forget_processes(self, killed=(), update_lease=True):
        """Drop the recorded processes, except any that a failed kill left alive.

        A process that was requested but not killed and still has its recorded identity
        stays in memory and in owner.json, so ``ie_kill_orphans`` can still find it.
        Returns True when something was kept.
        """
        keep_driver = bool(self.driver_pid) and self.driver_pid not in killed and \
            _same_process(self.driver_pid, self.driver_identity)
        keep_edge = bool(self.edge_pid) and self.edge_pid not in killed and \
            _same_process(self.edge_pid, self.edge_identity)
        if not keep_driver:
            self.driver_pid = self.driver_identity = None
        if not keep_edge:
            self.edge_pid = self.edge_identity = self.profile = None
        if keep_driver or keep_edge:
            log(f"kept for ie_kill_orphans (kill failed): driver={self.driver_pid} edge={self.edge_pid}")
        if update_lease and LEASE.acquired:
            try:
                LEASE.update(driver_pid=self.driver_pid, edge_pid=self.edge_pid,
                             profile=self.profile, driver_identity=self.driver_identity,
                             edge_identity=self.edge_identity)
            except Exception as exc:
                log(f"could not update the owner record: {exc}")
        return keep_driver or keep_edge

    def quit_browser(self):
        """Quit the driver and kill our own Edge, but keep the lease (and the identity)."""
        driver, self.driver = self.driver, None
        self.main_hwnd = None
        self._quit_driver(driver, QUIT_TIMEOUT)
        # IEDriver often leaves the IE-mode Edge window alive: kill ours, if still ours.
        self._forget_processes(self.kill_own_processes())

    def force_kill(self):
        """Stop everything at once without ever calling the driver (``ie_close(force=true)``).

        Wait loops are cancelled first, the processes are killed by verified identity, and
        the driver reference is dropped. The lease stays: this process still owns the name.
        """
        self.cancel.set()
        killed = self.kill_own_processes()
        self.driver = None
        self.main_hwnd = None
        self._forget_processes(killed)
        return killed

    def close(self, timeout_s=10):
        """Final teardown at exit: cancel waits, quit (bounded), kill, release the lease.

        If a kill failed and the process is still alive, the lease is left in place (this
        process is exiting): the next server takes it over as stale and keeps the record, so
        ``ie_kill_orphans`` can still find the survivor.
        """
        self.cancel.set()
        driver, self.driver = self.driver, None
        self.main_hwnd = None
        self._quit_driver(driver, timeout_s)
        survivors = self._forget_processes(self.kill_own_processes())
        if not survivors:
            release_lock()

    # -- helpers --
    def _by(self, by):
        from selenium.webdriver.common.by import By
        m = {
            "id": By.ID, "css": By.CSS_SELECTOR, "xpath": By.XPATH,
            "name": By.NAME, "link_text": By.LINK_TEXT, "tag": By.TAG_NAME,
            "class": By.CLASS_NAME,
        }
        if by not in m:
            raise ValueError(f"unknown 'by': {by} (use one of {sorted(m)})")
        return m[by]

    def _apply_frame(self, frame):
        """Switch into a frame. Accepts a single index/name, or a nested PATH —
        a list, or a string with '/' separators (e.g. "3/0" or "3/inner") — to reach
        frames inside frames, e.g. "3/0" = the 1st frame inside the 4th frame."""
        d = self.driver
        d.switch_to.default_content()
        if frame is None or frame == "":
            return
        if isinstance(frame, (list, tuple)):
            segments = list(frame)
        elif isinstance(frame, str) and ("/" in frame):
            segments = [s for s in frame.split("/") if s != ""]
        else:
            segments = [frame]
        for seg in segments:
            if isinstance(seg, int):
                d.switch_to.frame(seg)
            elif isinstance(seg, str) and seg.isdigit():
                d.switch_to.frame(int(seg))
            else:
                d.switch_to.frame(seg)

    def find(self, by, value, frame=None, timeout=10, interval=0.5):
        """Find an element, auto-waiting up to `timeout`s for it to appear (like
        Playwright). Re-applies the frame each poll so a mid-load reload recovers."""
        from selenium.common.exceptions import NoSuchElementException
        d = self.ensure()
        deadline = time.time() + max(0, timeout)
        while True:
            self.check_cancel()  # outside the try: a cancel must not look like "not found yet"
            try:
                self._apply_frame(frame)
                els = d.find_elements(self._by(by), value)
                if els:
                    return els[0]
            except Exception:
                pass
            if time.time() >= deadline:
                raise NoSuchElementException(
                    f"no element {by}={value!r} (frame={frame}) within {timeout}s")
            self.pause(interval)


SESSION = IeSession()
