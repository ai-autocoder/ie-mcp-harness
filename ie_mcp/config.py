"""Settings: every environment variable the server reads, parsed once.

Code reads ``config.SETTINGS`` at call time (``from ie_mcp import config`` then
``config.SETTINGS.x``), never ``from ie_mcp.config import SETTINGS``, so a test can
replace it. Each later feature adds its env vars here.

Configuration (all via env vars, all optional):
  IE_EDGE_PATH      path to msedge.exe
  IE_DRIVER_PATH    path to IEDriverServer.exe. Unset: the highest version in the per-user
                    Selenium cache (~/.cache/selenium/IEDriverServer/win32, else win64)
  IE_SITE_LIST      path to an Edge IE-mode site list XML. If set, the server
                    verifies the HKCU Edge policy points at it (so listed sites
                    render in IE mode). If unset, the server manages no policy
                    and assumes Edge is already configured for IE mode.
  IE_DEFAULT_URL    URL ie_open uses when none is given (default about:blank)
  IE_ATTACH_RETRIES IEDriver attach attempts (default 3)
  IE_PAGE_TIMEOUT   per-page load timeout in seconds (default 30)
  IE_PYDEPS         extra sys.path dir for vendored selenium (unset: nothing is added)
  IE_INTERACTION_MODE background (default: native input sent without taking window focus)
                    or native (requires window focus; fallback)
  IE_CLEAN_SESSION  1/true/yes makes IEDriver clear the user's whole IE-mode cookie store
                    and cache at session start and end (default off)
  IE_UPLOAD_DIR     directory ie_upload may read from (unset: uploads are disabled)
  IE_SESSION_ID     logical agent/session name (default pid-<mcp process PID>)
  IE_SESSION_ROOT   root for per-session logs, locks, and ownership records
  IE_LOG_FILE       log file (default <session directory>/ie-mcp.log)
  IE_LOCK_FILE      session lock file (default <session directory>/session.lock)
  IE_NO_LOCK        1/true/yes disables the session lease (debugging only)
"""
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

DEFAULT_EDGE_PATH = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

DRIVER_EXE = "IEDriverServer.exe"
# Selenium Manager's per-user cache has one folder per architecture, then one per version.
# 32-bit (win32) is searched first: it is the reference build for IE mode.
DRIVER_ARCHITECTURES = ("win32", "win64")


def _version_key(name):
    """Sort key for a folder named like ``4.14.0``; non-numeric names sort lowest."""
    parts = name.split(".")
    # isascii: str.isdigit() is true for "²" etc., which int() rejects.
    if all(part.isascii() and part.isdigit() for part in parts):
        return (1, tuple(int(part) for part in parts))
    return (0, ())


def discover_driver_path(env: Mapping[str, str], home: Path) -> str | None:
    """Find IEDriverServer.exe.

    ``IE_DRIVER_PATH`` wins: it is returned if the file exists, else FileNotFoundError is
    raised (a typo must not silently fall back to some other driver). Otherwise the highest
    version in ``<home>/.cache/selenium/IEDriverServer/win32/<version>/``, then the same
    under ``win64``; None when nothing is found.
    """
    explicit = env.get("IE_DRIVER_PATH")
    if explicit:
        if not os.path.isfile(explicit):
            raise FileNotFoundError(f"IE_DRIVER_PATH does not point to a file: {explicit}")
        return explicit
    root = Path(home) / ".cache" / "selenium" / "IEDriverServer"
    for architecture in DRIVER_ARCHITECTURES:
        try:
            candidates = [d for d in (root / architecture).iterdir()
                          if (d / DRIVER_EXE).is_file()]
        except OSError:
            continue
        if candidates:
            best = max(candidates, key=lambda d: _version_key(d.name))
            return str(best / DRIVER_EXE)
    return None


@dataclass(frozen=True)
class InteractionMode:
    name: str
    require_window_focus: bool
    native_events: bool


def resolve_interaction_mode(env=None):
    """Resolve the IEDriver input strategy from the process environment."""
    env = os.environ if env is None else env
    name = (env.get("IE_INTERACTION_MODE") or "background").strip().lower()
    if name == "native":
        return InteractionMode(name="native", require_window_focus=True, native_events=True)
    if name == "background":
        # Spike D17: synthetic events (native_events=False) did nothing on the development
        # machine, so "background" is native input that does not require window focus.
        return InteractionMode(name="background", require_window_focus=False, native_events=True)
    raise ValueError("IE_INTERACTION_MODE must be native or background")


@dataclass(frozen=True)
class SessionPaths:
    session_id: str
    root: Path
    directory: Path
    log_file: Path
    lock_file: Path
    owner_file: Path


def _safe_session_id(value):
    session_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value)).strip("-.")
    if not session_id:
        raise ValueError("IE_SESSION_ID must contain at least one letter or number")
    return session_id[:80]


def build_session_paths(env=None, pid=None):
    """Resolve all runtime artifacts beneath one MCP process's session directory."""
    env = os.environ if env is None else env
    pid = os.getpid() if pid is None else int(pid)
    session_id = _safe_session_id(env.get("IE_SESSION_ID") or f"pid-{pid}")
    root = Path(env.get("IE_SESSION_ROOT") or Path(tempfile.gettempdir()) / "ie-mcp" / "sessions")
    directory = root / session_id
    return SessionPaths(
        session_id=session_id,
        root=root,
        directory=directory,
        log_file=Path(env.get("IE_LOG_FILE") or directory / "ie-mcp.log"),
        lock_file=Path(env.get("IE_LOCK_FILE") or directory / "session.lock"),
        owner_file=directory / "owner.json",
    )


@dataclass(frozen=True)
class Settings:
    edge_path: str
    # An explicit IE_DRIVER_PATH (not checked here) or the discovered driver; None if none found.
    driver_path: str | None
    site_list: str | None  # optional; no app-specific default
    default_url: str
    attach_retries: int
    page_timeout: int
    use_lock: bool
    pydeps: Path | None  # only IE_PYDEPS adds a path; there is no implicit default
    clean_session: bool  # opt-in: wipes the user's whole IE-mode cookie store and cache
    upload_dir: Path | None  # None disables ie_upload
    interaction_mode: InteractionMode
    session_paths: SessionPaths


def _flag(env, name):
    return env.get(name, "").strip().lower() in ("1", "true", "yes")


def _optional_path(env, name):
    value = env.get(name, "").strip()
    return Path(value) if value else None


def _optional_absolute_path(env, name):
    """Like _optional_path, but made absolute once, here: a relative directory would
    otherwise follow the process's working directory, which can change between calls."""
    path = _optional_path(env, name)
    return path.resolve() if path else None


def load_settings(env: Mapping[str, str], pid: int | None = None,
                  home: Path | None = None) -> Settings:
    """Parse every env var once. Raises ValueError on an invalid value, as upstream did at import.

    An ``IE_DRIVER_PATH`` that does not exist is not an error here (the server must still
    start and list its tools); the session reports it when it tries to launch the driver.
    """
    driver_path = env.get("IE_DRIVER_PATH") or discover_driver_path(
        env, Path.home() if home is None else home)
    return Settings(
        edge_path=env.get("IE_EDGE_PATH", DEFAULT_EDGE_PATH),
        driver_path=driver_path,
        site_list=env.get("IE_SITE_LIST"),
        default_url=env.get("IE_DEFAULT_URL", "about:blank"),
        attach_retries=int(env.get("IE_ATTACH_RETRIES", "3")),
        page_timeout=int(env.get("IE_PAGE_TIMEOUT", "30")),
        use_lock=env.get("IE_NO_LOCK", "").lower() not in ("1", "true", "yes"),
        pydeps=_optional_path(env, "IE_PYDEPS"),
        clean_session=_flag(env, "IE_CLEAN_SESSION"),
        upload_dir=_optional_absolute_path(env, "IE_UPLOAD_DIR"),
        interaction_mode=resolve_interaction_mode(env),
        session_paths=build_session_paths(env, pid),
    )


SETTINGS = load_settings(os.environ)

# Make a vendored selenium importable regardless of how we are launched.
if SETTINGS.pydeps and str(SETTINGS.pydeps) not in sys.path:
    sys.path.insert(0, str(SETTINGS.pydeps))
