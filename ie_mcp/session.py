"""Session ownership, process tracking and the long-lived IEDriver session.

One MCP process owns one browser. Settings are read from ``config.SETTINGS`` at call
time, never copied into module globals.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from ie_mcp import config
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


# --- per-agent ownership (one MCP process owns one browser) ------------------
def _pid_alive(pid):
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True, timeout=10).stdout
        return str(pid) in out
    except Exception:
        return False


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
        self.pid_alive = pid_alive or _pid_alive
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
                try:
                    self.paths.lock_file.unlink()
                except FileNotFoundError:
                    pass
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, sort_keys=True)
                fh.write("\n")
            self.acquired = True
            self._record(driver_pid=None, edge_pid=None, profile=None)
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

    Returns a list of dicts: {pid, kind, profile, url}. Identifies IE-mode Edge by
    the --ie-mode-force flag and pulls the per-session IEDriver-<uuid> profile and
    the launch URL from the command line. Uses PowerShell/CIM (Windows-only).
    """
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' or Name='IEDriverServer.exe'\" |"
        " ForEach-Object { $_.Name + '|' + $_.ProcessId + '|' + ($_.CommandLine -replace '[\\r\\n]',' ') }"
    )
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=20).stdout
    except Exception as exc:
        log(f"list_ie_browsers failed: {exc}")
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split("|", 2)
        if len(parts) < 2:
            continue
        name, pid = parts[0].strip(), parts[1].strip()
        cmd = parts[2] if len(parts) > 2 else ""
        if not pid.isdigit():
            continue
        pid = int(pid)
        if name.lower() == "iedriverserver.exe":
            rows.append({"pid": pid, "kind": "iedriver", "profile": None, "url": None})
            continue
        # only the main IE-mode browser process (skip helper --type=... procs)
        if "--ie-mode-force" not in cmd or "--type=" in cmd:
            continue
        prof = re.search(r"IEDriver-([0-9a-fA-F-]+)", cmd)
        url = re.search(r"(https?://\S+)", cmd)
        rows.append({"pid": pid, "kind": "edge-ie",
                     "profile": prof.group(1) if prof else None,
                     "url": url.group(1) if url else None})
    return rows


def kill_pids(pids):
    killed = []
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, text=True, timeout=15)
            killed.append(pid)
        except Exception as exc:
            log(f"kill {pid} failed: {exc}")
    return killed


def _owned_processes_for_record(record, browser_by_pid):
    """Return PIDs that still match a stale record strongly enough to kill safely."""
    profile = record.get("profile")
    edge_pid = record.get("edge_pid")
    driver_pid = record.get("driver_pid")
    edge = browser_by_pid.get(edge_pid)
    driver = browser_by_pid.get(driver_pid)

    edge_matches = bool(
        edge
        and edge.get("kind") == "edge-ie"
        and profile
        and edge.get("profile") == profile
    )
    owned = []
    # The Edge profile is the durable identity for an IEDriver-created browser.
    # Only trust the associated driver PID when that profile still exists too;
    # this prevents PID reuse from killing a newer agent's unrelated driver.
    if edge_matches and driver and driver.get("kind") == "iedriver":
        owned.append(driver_pid)
    if edge_matches:
        owned.append(edge_pid)
    return owned


def cleanup_stale_sessions(session_root, browser_rows=None, pid_alive=None, kill=None):
    """Kill only browsers registered to dead ie-mcp owner processes.

    Unknown browser processes and sessions whose owner PID is alive are always
    preserved. Session records remain on disk with cleared process identities so
    their scoped logs are retained for diagnosis.
    """
    session_root = Path(session_root)
    pid_alive = pid_alive or _pid_alive
    kill = kill or kill_pids
    browser_rows = list_ie_browsers() if browser_rows is None else list(browser_rows)
    browser_by_pid = {row.get("pid"): row for row in browser_rows if row.get("pid")}
    cleanup_pids = []
    stale_records = []
    skipped_live_sessions = []

    if session_root.exists():
        for owner_file in sorted(session_root.glob("*/owner.json")):
            record = _read_json(owner_file)
            session_id = record.get("session_id") or owner_file.parent.name
            owner_pid = record.get("owner_pid")
            if isinstance(owner_pid, int) and pid_alive(owner_pid):
                skipped_live_sessions.append(session_id)
                continue
            owned = _owned_processes_for_record(record, browser_by_pid)
            if owned:
                cleanup_pids.extend(pid for pid in owned if pid not in cleanup_pids)
                stale_records.append((owner_file, record))

    killed = list(kill(cleanup_pids)) if cleanup_pids else []
    for owner_file, record in stale_records:
        record.update({
            "driver_pid": None,
            "edge_pid": None,
            "profile": None,
            "cleaned_at": time.time(),
        })
        _write_json_atomic(owner_file, record)

    cleanup_set = set(cleanup_pids)
    return {
        "killed": killed,
        "skipped_live_sessions": sorted(skipped_live_sessions),
        "untracked_process_pids": sorted(
            row["pid"] for row in browser_rows if row.get("pid") not in cleanup_set
        ),
    }


# --- the IE driver wrapper ---------------------------------------------------
class IeSession:
    def __init__(self):
        self.driver = None
        self.driver_pid = None    # IEDriverServer.exe PID
        self.edge_pid = None      # the IE-mode msedge.exe window PID we own
        self.profile = None       # IEDriver-<uuid> temp profile that identifies us

    def _build(self, initial_url):
        from selenium import webdriver
        from selenium.webdriver.ie.service import Service

        settings = config.SETTINGS
        options = webdriver.IeOptions()
        options.attach_to_edge_chrome = True
        options.edge_executable_path = settings.edge_path
        options.ignore_zoom_level = True
        options.ignore_protected_mode_settings = True
        options.require_window_focus = settings.interaction_mode.require_window_focus
        options.ensure_clean_session = True
        options.native_events = settings.interaction_mode.native_events
        options.browser_attach_timeout = 30_000
        options.page_load_strategy = "none"
        # CRITICAL for IE mode: navigate during session creation so IEDriver
        # crosses the Protected-Mode boundary and reconnects to the new browser
        # process *before* handing back control. Navigating afterwards makes the
        # driver lose (and exit on) the browser.
        if initial_url:
            options.initial_browser_url = initial_url

        service = Service(executable_path=settings.driver_path)
        return webdriver.Ie(service=service, options=options)

    def ensure(self, initial_url=None):
        """Return a live driver, (re)creating it with retries if needed.

        When a new session must be created, initial_url is opened as part of
        session creation (the only reliable way into IE mode).
        """
        from selenium.common.exceptions import WebDriverException

        if self.driver is not None:
            try:
                _ = self.driver.current_url  # cheap liveness probe
                return self.driver
            except WebDriverException:
                log("session dead, recreating")
                self.quit()

        acquire_lock()  # refuse only if another process owns this session identity
        retries = config.SETTINGS.attach_retries
        last = None
        for attempt in range(1, retries + 1):
            try:
                log(f"creating IE-mode session (attempt {attempt}/{retries}) url={initial_url}")
                before = {b["profile"] for b in list_ie_browsers() if b["kind"] == "edge-ie"}
                self.driver = self._build(initial_url)
                self.driver.set_page_load_timeout(config.SETTINGS.page_timeout)
                _ = self.driver.current_url  # verify it survived the boundary crossing
                self._capture_identity(before)
                return self.driver
            except Exception as exc:
                last = exc
                log(f"attach failed: {exc}")
                self.quit()
                time.sleep(2)
        raise RuntimeError(f"could not start Edge IE mode after {retries} tries: {last}")

    def _capture_identity(self, before_profiles):
        """Record which browser we just opened (driver pid + the new IE-mode edge)."""
        try:
            self.driver_pid = self.driver.service.process.pid
        except Exception:
            self.driver_pid = None
        self.edge_pid = self.profile = None
        try:
            for b in list_ie_browsers():
                if b["kind"] == "edge-ie" and b["profile"] not in before_profiles:
                    self.edge_pid, self.profile = b["pid"], b["profile"]
                    break
        except Exception:
            pass
        LEASE.update(driver_pid=self.driver_pid, edge_pid=self.edge_pid, profile=self.profile)
        log(f"session identity: driver_pid={self.driver_pid} edge_pid={self.edge_pid} profile={self.profile}")

    def quit(self):
        if self.driver is not None:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None
        # IEDriver often leaves the IE-mode Edge window alive — kill ours by pid.
        if self.edge_pid:
            kill_pids([self.edge_pid])
        self.driver_pid = self.edge_pid = self.profile = None
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
            time.sleep(interval)


SESSION = IeSession()
