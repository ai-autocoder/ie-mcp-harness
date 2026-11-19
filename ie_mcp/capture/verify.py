"""Pure helpers of the capture wiring (plan Task 13): which requests prove the browser uses the
proxy, which hosts a pattern list covers, and the read-only ``ProxyOverride`` reader.

Nothing here touches the session, the settings or the network; the registry is only read.
"""
import fnmatch
import urllib.parse

DEFAULT_PORTS = {"http": 80, "https": 443}
INTERNET_SETTINGS = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"


# --- URL matching ----------------------------------------------------------------------------
def http_target(url):
    """``(scheme, host, port)`` of an http(s) URL (scheme and host lower case, default port
    filled in), or None for anything else: ``about:``, ``file:``, ``javascript:``, nothing."""
    if not isinstance(url, str):
        return None
    try:
        parts = urllib.parse.urlsplit(url.strip())
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS or not host:
        return None
    return scheme, host.lower(), port or DEFAULT_PORTS[scheme]


def _authority(host, port):
    return ("[%s]" % host if ":" in host else host) + ":%d" % port


def url_seen(url, seen):
    """True if the proxy's ``seen`` list shows the browser requesting ``url``.

    Both sides are normalised: scheme and host case, a default port, the fragment (never sent)
    and an empty path (``/``). A query that IE re-encoded still counts on host, port and path.
    For https the proxy only sees a CONNECT to ``host:port``, so that is what is looked for.
    """
    target = http_target(url)
    if target is None:
        return False
    scheme, host, port = target
    if scheme == "https":
        wanted = _authority(host, port)
        return any(isinstance(item, str) and item.strip().lower() == wanted for item in seen)
    parts = urllib.parse.urlsplit(url.strip())
    path = parts.path or "/"
    for item in seen:
        if http_target(item) != target:
            continue
        other = urllib.parse.urlsplit(item.strip())
        if (other.path or "/") == path:
            return True  # same path; the query is what IE may have re-encoded
    return False


# --- host patterns -------------------------------------------------------------------------
def host_matches(host, patterns):
    """True if ``host`` matches any of the (case-insensitive, ``*``/``?``) ``patterns``."""
    host = host.lower()
    return any(fnmatch.fnmatchcase(host, str(pattern).strip().lower()) for pattern in patterns
               if str(pattern).strip())


def override_matches(host, entries):
    """True if a ``ProxyOverride`` list would make IE bypass the proxy for ``host``.

    An entry is a host pattern (``*.corp.example``, ``10.*``), optionally with a scheme and a
    port (both ignored here), or ``<local>``: every host without a dot. Other ``<...>`` tokens
    (``<-loopback>``) are not host patterns and are skipped.
    """
    host = host.lower()
    for raw in entries:
        entry = str(raw).strip().lower()
        if not entry:
            continue
        if entry == "<local>":
            if "." not in host:
                return True
            continue
        if entry.startswith("<"):
            continue
        entry = entry.split("://", 1)[-1]
        head, sep, tail = entry.rpartition(":")
        if sep and tail.isdigit() and ":" not in head:
            entry = head
        if fnmatch.fnmatchcase(host, entry):
            return True
    return False


def read_proxy_override():
    """The entries of ``ProxyOverride`` (lower case, in order) from the current user's Internet
    Settings. Read-only: ``OpenKey`` with ``KEY_READ`` and ``QueryValueEx``, never a write call.
    A missing key or value, or a platform without ``winreg``, means no entries."""
    try:
        import winreg
    except ImportError:
        return []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, INTERNET_SETTINGS, 0,
                            winreg.KEY_READ) as key:
            value, _kind = winreg.QueryValueEx(key, "ProxyOverride")
    except OSError:
        return []
    return [part.strip().lower() for part in str(value).split(";") if part.strip()]


# --- settings parsing ----------------------------------------------------------------------
def parse_bypass(text):
    """``IE_CAPTURE_BYPASS``: comma-separated host patterns, stripped, empties dropped."""
    return tuple(part.strip() for part in (text or "").split(",") if part.strip())


def parse_upstream_proxy(text):
    """``IE_CAPTURE_UPSTREAM_PROXY`` as ``(host, port)``; ``host:port`` or ``http://host:port``.

    Raises ValueError for anything else (a malformed value must not silently become "no proxy",
    which would send corporate traffic around the proxy it was meant to use).
    """
    value = (text or "").strip()
    if not value:
        return None
    message = "IE_CAPTURE_UPSTREAM_PROXY must be host:port (for example gateway.example.org:3128)"
    rest = value.split("://", 1)[-1].rstrip("/")
    host, sep, port = rest.rpartition(":")
    if not sep or not host or not port.isascii() or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(message)
    return host, int(port)
