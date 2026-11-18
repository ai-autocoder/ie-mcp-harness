"""Settings: every environment variable the server reads, parsed once.

Code reads ``config.SETTINGS`` at call time (``from ie_mcp import config`` then
``config.SETTINGS.x``), never ``from ie_mcp.config import SETTINGS``, so a test can
replace it. Each later feature adds its env vars here.

Configuration (all via env vars, all optional):
  IE_EDGE_PATH      path to msedge.exe
  IE_DRIVER_PATH    path to IEDriverServer.exe
  IE_SITE_LIST      path to an Edge IE-mode site list XML. If set, the server
                    verifies the HKCU Edge policy points at it (so listed sites
                    render in IE mode). If unset, the server manages no policy
                    and assumes Edge is already configured for IE mode.
  IE_DEFAULT_URL    URL ie_open uses when none is given (default about:blank)
  IE_ATTACH_RETRIES IEDriver attach attempts (default 3)
  IE_PAGE_TIMEOUT   per-page load timeout in seconds (default 30)
  IE_PYDEPS         extra sys.path dir for vendored selenium (default ../.pydeps,
                    relative to the repository directory)
  IE_INTERACTION_MODE native (focused OS input, default) or background
                    (synthetic input without forcing browser focus)
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
DEFAULT_DRIVER_PATH = (
    r"C:\Users\tigre\.cache\selenium\IEDriverServer\win32\4.14.0\IEDriverServer.exe"
)

# Upstream looked for ../.pydeps next to ie_mcp.py, i.e. beside the repository directory.
_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PYDEPS = os.path.join(os.path.dirname(_PROJECT_DIR), ".pydeps")


@dataclass(frozen=True)
class InteractionMode:
    name: str
    require_window_focus: bool
    native_events: bool


def resolve_interaction_mode(env=None):
    """Resolve the IEDriver input strategy from the process environment."""
    env = os.environ if env is None else env
    name = (env.get("IE_INTERACTION_MODE") or "native").strip().lower()
    if name == "native":
        return InteractionMode(name="native", require_window_focus=True, native_events=True)
    if name == "background":
        return InteractionMode(name="background", require_window_focus=False, native_events=False)
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
    driver_path: str
    site_list: str | None  # optional; no app-specific default
    default_url: str
    attach_retries: int
    page_timeout: int
    use_lock: bool
    pydeps: str
    interaction_mode: InteractionMode
    session_paths: SessionPaths


def load_settings(env: Mapping[str, str], pid: int | None = None) -> Settings:
    """Parse every env var once. Raises ValueError on an invalid value, as upstream did at import."""
    return Settings(
        edge_path=env.get("IE_EDGE_PATH", DEFAULT_EDGE_PATH),
        driver_path=env.get("IE_DRIVER_PATH", DEFAULT_DRIVER_PATH),
        site_list=env.get("IE_SITE_LIST"),
        default_url=env.get("IE_DEFAULT_URL", "about:blank"),
        attach_retries=int(env.get("IE_ATTACH_RETRIES", "3")),
        page_timeout=int(env.get("IE_PAGE_TIMEOUT", "30")),
        use_lock=env.get("IE_NO_LOCK", "").lower() not in ("1", "true", "yes"),
        pydeps=env.get("IE_PYDEPS", DEFAULT_PYDEPS),
        interaction_mode=resolve_interaction_mode(env),
        session_paths=build_session_paths(env, pid),
    )


SETTINGS = load_settings(os.environ)

# Make a vendored selenium importable regardless of how we are launched.
if SETTINGS.pydeps and SETTINGS.pydeps not in sys.path:
    sys.path.insert(0, SETTINGS.pydeps)
