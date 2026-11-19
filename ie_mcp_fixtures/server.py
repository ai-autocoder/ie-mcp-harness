"""Two-origin HTTP server for the legacy fixture site.

The fixture site reproduces legacy IE-only patterns (framesets, old document
modes, showModalDialog, ActiveX XMLHTTP, form posts, windows-1252 pages) so the
harness can be tested without any real application. It is served on two
origins: ``http://<host>:<port>/`` and ``http://localhost:<alt_port>/``. IE's
same-origin rule ignores the port, so the second origin uses another host name.

Every response is marked ``Cache-Control: no-store`` and carries no
validators, so nothing the tests load lands in the per-user WinINet cache that
the user's own IE-mode browsing shares.
"""

from __future__ import annotations

import errno
import html
import http.server
import itertools
import mimetypes
import socket
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

SITE_DIR = Path(__file__).resolve().parent / "site"

ALT_BASE_PLACEHOLDER = b"{{ALT_BASE}}"
MAX_BODY_BYTES = 10 * 1024 * 1024
MAX_SLOW_MS = 60_000

LOOKUP_ITEMS = (("A1", "Alpha"), ("B2", "Beta"), ("C3", "Gamma"))

FRAGMENT_BODY = b"OK|A1"

BIG_SIZE = 1024 * 1024
BIG_CHUNK = 8192
BIG_PATTERN = (b"0123456789abcdef" * (BIG_CHUNK // 16))

PROGRESSIVE_GAP = 0.05  # seconds between the head writes
POLL_INTERVAL = 0.05  # seconds; listener poll, bounds how long stop() waits
PROGRESSIVE_PARTS = (
    b'<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN" '
    b'"http://www.w3.org/TR/html4/loose.dtd"><html><head><title>progressive</title>',
    b'<meta http-equiv="X-UA-Compatible" content="IE=7">',
    b'<link rel="stylesheet" href="data:text/css,">',
    b"</head><body>progressive ok</body></html>",  # sent after the ms delay
)


class HeaderDict(dict):
    """Read-only mapping of header names to values; names compare case-insensitively.

    Iteration yields the names as the client sent them; a repeated header keeps
    its last value. It is built once per request and never changed, so every
    mutator raises TypeError (a mutable version would need to keep the
    lower-case index in sync on every path, including update/pop/setdefault).
    """

    def __init__(self, items=()):
        merged = {}
        names = {}
        for name, value in items:
            old = names.get(name.lower())
            if old is not None and old != name:
                del merged[old]
            names[name.lower()] = name
            merged[name] = value
        super().__init__(merged)
        self._names = names

    def __getitem__(self, name):
        if isinstance(name, str):
            name = self._names.get(name.lower(), name)
        return super().__getitem__(name)

    def __contains__(self, name):
        return isinstance(name, str) and name.lower() in self._names

    def get(self, name, default=None):
        return self[name] if name in self else default

    def copy(self):
        return HeaderDict(self.items())

    __copy__ = copy

    def __deepcopy__(self, memo):
        return HeaderDict(self.items())  # names and values are immutable str

    def __reduce__(self):
        return (HeaderDict, (list(self.items()),))

    def _read_only(self, *args, **kwargs):
        raise TypeError("HeaderDict is read-only")

    __setitem__ = __delitem__ = __ior__ = _read_only
    update = pop = popitem = setdefault = clear = _read_only


@dataclass(frozen=True)
class RequestRecord:
    """One request either listener received."""

    method: str
    path: str
    headers: HeaderDict  # case-insensitive names
    conn_id: int  # identifies the client TCP connection

# HTML pages are sent without a charset parameter on purpose: a header charset
# would override the page's own <meta> declaration, and the fixtures exist to
# exercise how IE detects the charset of legacy pages from the meta.
CONTENT_TYPES = {
    ".htm": "text/html",
    ".html": "text/html",
    ".xml": "text/xml",
    ".js": "application/javascript",
    ".css": "text/css",
    ".gif": "image/gif",
    ".png": "image/png",
    ".txt": "text/plain",
}


def _url(host: str, port: int) -> str:
    if ":" in host and not host.startswith("["):
        host = "[" + host + "]"
    return "http://%s:%d/" % (host, port)


def _decode_form_value(raw: str) -> str:
    """Decode a percent-encoded form value.

    Browsers encode a form post in the page's charset, so a windows-1252 page
    posts bytes that are not valid UTF-8. Try UTF-8 first, then windows-1252.
    """
    data = urllib.parse.unquote_to_bytes(raw.replace("+", " "))
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("windows-1252", errors="replace")


def _parse_form(body: bytes) -> list[tuple[str, str]]:
    text = body.decode("latin-1")
    fields = []
    for pair in text.split("&"):
        if not pair:
            continue
        name, _, value = pair.partition("=")
        fields.append((_decode_form_value(name), _decode_form_value(value)))
    return fields


class _FixtureHandler(http.server.BaseHTTPRequestHandler):
    # HTTP/1.1 so clients (and the recording proxy in later tasks) can keep a
    # connection alive across requests. Every response sets Content-Length.
    protocol_version = "HTTP/1.1"
    server_version = "ie-mcp-fixtures"
    # Idle keep-alive connections are dropped after this many seconds.
    timeout = 30

    alt_base_url = ""  # set on a per-server subclass
    fixture = None     # the owning FixtureServer, set on a per-server subclass

    _cache_header_sent = False
    conn_id = 0

    def setup(self):
        super().setup()
        # One handler instance serves one client TCP connection (all of its
        # keep-alive requests), so the connection id is assigned here.
        self.conn_id = self.fixture._next_conn_id()
        # False once stop() has begun: this connection then gets no reply.
        self._accepted = self.fixture._track(self.connection)

    def handle(self):
        if self._accepted:
            super().handle()

    def finish(self):
        try:
            super().finish()
        finally:
            self.fixture._untrack(self.connection)

    def parse_request(self):
        ok = super().parse_request()
        if ok:
            self.fixture._record(RequestRecord(
                method=self.command, path=self.path,
                headers=HeaderDict(self.headers.items()), conn_id=self.conn_id))
        return ok

    def version_string(self):
        return self.server_version

    # Every response, including the base class's send_error() replies
    # (404/501/400...), must carry Cache-Control: no-store.
    def send_header(self, keyword, value):
        if keyword.lower() == "cache-control":
            self._cache_header_sent = True
        super().send_header(keyword, value)

    def end_headers(self):
        if not self._cache_header_sent:
            self.send_header("Cache-Control", "no-store")
        self._cache_header_sent = False
        super().end_headers()

    def log_message(self, format, *args):  # noqa: A002 - signature from base class
        pass

    # -- helpers ---------------------------------------------------------

    def _send(self, status, content_type, body, headers=()):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_text(self, status, text):
        self._send(status, "text/plain; charset=utf-8", text.encode("utf-8"))

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > MAX_BODY_BYTES:
            raise ValueError("request body too large")
        return self.rfile.read(length) if length else b""

    # -- routing ---------------------------------------------------------

    def do_GET(self):
        parts = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
        if parts.path == "/api/lookup.xml":
            self._lookup(query.get("q", [""])[0])
        elif parts.path == "/slow":
            self._slow(query.get("ms", ["0"])[0])
        elif parts.path == "/cookie":
            self._send(200, "text/plain; charset=utf-8", b"cookie set",
                       [("Set-Cookie", "sid=fixture-session; Path=/")])
        elif parts.path == "/fragment":
            # A bare HTML fragment, as legacy XHR endpoints return: text/html
            # but no tags, so a proxy must never inject into it.
            self._send(200, "text/html", FRAGMENT_BODY)
        elif parts.path == "/big":
            self._big(query.get("ms", ["0"])[0])
        elif parts.path == "/progressive":
            self._progressive(query.get("ms", ["0"])[0])
        else:
            self._static(parts.path)

    do_HEAD = do_GET

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        try:
            body = self._read_body()
        except ValueError as exc:
            self.close_connection = True
            self._send_text(413, str(exc))
            return
        if path == "/echo":
            self._echo(body)
        else:
            self._send_text(405, "POST is only accepted on /echo")

    # -- endpoints -------------------------------------------------------

    def _static(self, url_path):
        rel = urllib.parse.unquote(url_path).lstrip("/") or "index.htm"
        target = (SITE_DIR / rel).resolve()
        if SITE_DIR not in target.parents or not target.is_file():
            self._send_text(404, "not found")
            return
        body = target.read_bytes()
        suffix = target.suffix.lower()
        if suffix in (".htm", ".html"):
            body = body.replace(ALT_BASE_PLACEHOLDER,
                                self.alt_base_url.encode("ascii"))
        content_type = (CONTENT_TYPES.get(suffix)
                        or mimetypes.guess_type(target.name)[0]
                        or "application/octet-stream")
        self._send(200, content_type, body)

    def _echo(self, body):
        rows = []
        for name, value in _parse_form(body):
            rows.append('<dt>%s</dt><dd id="echo_%s">%s</dd>' % (
                html.escape(name), html.escape(name, quote=True),
                html.escape(value)))
        raw = html.escape(body.decode("latin-1"))
        page = (
            "<!DOCTYPE HTML PUBLIC \"-//W3C//DTD HTML 4.01 Transitional//EN\" "
            "\"http://www.w3.org/TR/html4/loose.dtd\">\n"
            "<html><head><title>Echo</title>"
            "<meta http-equiv=\"Content-Type\" content=\"text/html; charset=utf-8\">"
            "</head><body>\n<h1>Echo</h1>\n<dl>\n%s\n"
            "<dt>raw</dt><dd id=\"echo_raw\">%s</dd>\n</dl>\n"
            "<p><a id=\"back\" name=\"back\" href=\"javascript:history.back()\">Back</a></p>\n"
            "</body></html>\n" % ("\n".join(rows), raw))
        self._send(200, "text/html; charset=utf-8", page.encode("utf-8"))

    def _lookup(self, prefix):
        prefix = prefix.upper()
        items = "".join(
            '<item code="%s" name="%s"/>' % (code, name)
            for code, name in LOOKUP_ITEMS if code.startswith(prefix))
        xml = '<?xml version="1.0" encoding="utf-8"?><items>%s</items>' % items
        self._send(200, "text/xml; charset=utf-8", xml.encode("utf-8"))

    def _delay_ms(self, ms):
        """Parse an ``ms`` query value, or answer 400 and return None."""
        try:
            return max(0, min(int(ms), MAX_SLOW_MS))
        except ValueError:
            self._send_text(400, "ms must be an integer")
            return None

    def _start_stream(self, content_type, length):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.end_headers()

    def _slow(self, ms):
        delay = self._delay_ms(ms)
        if delay is None:
            return
        time.sleep(delay / 1000.0)
        self._send_text(200, "slow ok")

    def _big(self, ms):
        """1 MB of text/plain in 8 KB writes spread over ``ms``."""
        delay = self._delay_ms(ms)
        if delay is None:
            return
        self._start_stream("text/plain", BIG_SIZE)
        if self.command == "HEAD":
            return
        chunks = BIG_SIZE // BIG_CHUNK
        pause = delay / 1000.0 / max(1, chunks - 1)
        start = time.monotonic()
        for i in range(chunks):
            # Sleep until this write's deadline rather than a fixed pause: on
            # Windows a short sleep rounds up to the ~15.6 ms timer tick on some
            # Python versions, which would stretch the total far beyond ms.
            wait = start + i * pause - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.wfile.write(BIG_PATTERN)

    def _progressive(self, ms):
        """An IE7 document whose head arrives in separate small writes."""
        delay = self._delay_ms(ms)
        if delay is None:
            return
        # Without TCP_NODELAY, Nagle's algorithm could merge the small head
        # writes into one segment, and a reader would not see them apart.
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._start_stream("text/html", sum(len(p) for p in PROGRESSIVE_PARTS))
        if self.command == "HEAD":
            return
        last = len(PROGRESSIVE_PARTS) - 1
        for i, part in enumerate(PROGRESSIVE_PARTS):
            if i == last:
                time.sleep(delay / 1000.0)
            elif i:
                time.sleep(PROGRESSIVE_GAP)
            self.wfile.write(part)


class _ExclusiveHTTPServer(http.server.ThreadingHTTPServer):
    # HTTPServer sets allow_reuse_address, which on Windows maps to
    # SO_REUSEADDR and lets a second socket bind a port that is already in
    # use: two "origins" could silently end up on one port, or the fixture
    # server could share a port with another program. Bind exclusively.
    allow_reuse_address = False
    daemon_threads = True
    stopping = False

    def handle_error(self, request, client_address):
        # Browsers cancel loads and drop idle keep-alive connections all the
        # time; that is not a server error worth a traceback.
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        if self.stopping and isinstance(exc, OSError):
            return  # stop() closed this connection under the handler
        super().handle_error(request, client_address)

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class _ExclusiveHTTPServer6(_ExclusiveHTTPServer):
    address_family = socket.AF_INET6


# Windows reports a port held by another socket as WSAEADDRINUSE (10048), or
# WSAEACCES (10013) when the holder bound it with SO_EXCLUSIVEADDRUSE.
_PORT_IN_USE = {errno.EADDRINUSE, errno.EACCES, 10048, 10013}


def _port_in_use(exc: OSError) -> bool:
    return exc.errno in _PORT_IN_USE or getattr(exc, "winerror", None) in _PORT_IN_USE


class FixtureServer:
    """The fixture site on two origins.

    ``base_url`` is ``http://<host>:<port>/``. ``alt_base_url`` is
    ``http://localhost:<alt_port>/``: IE's same-origin rule ignores the port,
    so the second origin needs another host name, not only another port. The
    alt port listens on 127.0.0.1 and, when IPv6 loopback is available, on
    ::1, because "localhost" may resolve to either.

    Quirk: with default Internet Options, IE maps ``127.0.0.1`` to the
    Internet zone (Protected Mode on) and the dotless ``localhost`` to the
    Local intranet zone (Protected Mode off; intranet sites may also open in
    Compatibility View). The two origins are therefore on opposite sides of
    the Protected-Mode boundary: open alt-origin pages in their own session
    (``initial_browser_url`` on the alt origin) rather than navigating there,
    and record each origin's zone next to any cross-origin result.

    Both serve the same content. ``requests`` lists every request either
    listener received. Use it as a context manager, or call ``stop()``.
    """

    ALT_BIND_ATTEMPTS = 5

    def __init__(self, host: str = "127.0.0.1", port: int = 0, alt_port: int = 0):
        if port and port == alt_port:
            raise ValueError("port and alt_port must differ (two origins)")
        if host.lower() == "localhost":
            # The second origin is http://localhost:<alt_port>/; IE ignores ports
            # in its same-origin rule, so both would be one origin.
            raise ValueError("host must not be 'localhost' (it names the second origin)")
        handler = type("FixtureHandler", (_FixtureHandler,), {"fixture": self})
        self._lock = threading.Lock()
        self._requests: list[RequestRecord] = []
        self._connections: set = set()
        self._stopped = False
        self._conn_ids = itertools.count(1)
        self._servers = []
        try:
            self._servers.append(_ExclusiveHTTPServer((host, port), handler))
            self._servers.extend(self._bind_alt(alt_port, handler))
        except BaseException:
            for srv in self._servers:
                srv.server_close()
            raise
        self.base_url = _url(host, self._servers[0].server_address[1])
        self.alt_base_url = _url("localhost", self._servers[1].server_address[1])
        handler.alt_base_url = self.alt_base_url
        # A short poll interval keeps stop() fast: shutdown() waits for the
        # listener's next poll, which defaults to 0.5 s.
        self._threads = [
            threading.Thread(target=srv.serve_forever, name="fixture-server",
                             kwargs={"poll_interval": POLL_INTERVAL}, daemon=True)
            for srv in self._servers
        ]
        for thread in self._threads:
            thread.start()

    def _bind_alt(self, alt_port, handler):
        """Bind the alt port on 127.0.0.1 and, if available, on ::1.

        With ``alt_port=0`` the port is picked on 127.0.0.1; if another
        program already holds that port on ::1, try another port, so
        "localhost" can never reach someone else's server.
        """
        attempts = 1 if alt_port else self.ALT_BIND_ATTEMPTS
        for attempt in range(attempts):
            v4 = _ExclusiveHTTPServer(("127.0.0.1", alt_port), handler)
            if not socket.has_ipv6:
                return [v4]
            try:
                v6 = _ExclusiveHTTPServer6(("::1", v4.server_address[1]), handler)
            except OSError as exc:
                if _port_in_use(exc):
                    v4.server_close()
                    if attempt + 1 < attempts:
                        continue
                    raise
                # No IPv6 loopback on this machine: 127.0.0.1 alone is enough.
                return [v4]
            except BaseException:
                v4.server_close()
                raise
            return [v4, v6]
        raise OSError("could not bind the alt port on 127.0.0.1 and ::1")

    # -- request log -----------------------------------------------------

    def _next_conn_id(self) -> int:
        with self._lock:
            return next(self._conn_ids)

    def _record(self, record: RequestRecord) -> None:
        with self._lock:
            if not self._stopped:
                self._requests.append(record)

    def _track(self, conn) -> bool:
        """Register an accepted connection; False once stop() has begun.

        stop() sets ``_stopped`` and snapshots the connections under the same
        lock, so every connection is either in that snapshot (and closed by
        stop) or refused here - none is served after stop() returns.
        """
        with self._lock:
            if self._stopped:
                return False
            self._connections.add(conn)
            return True

    def _untrack(self, conn) -> None:
        with self._lock:
            self._connections.discard(conn)

    @property
    def requests(self) -> list[RequestRecord]:
        """A snapshot of every request received so far, oldest first."""
        with self._lock:
            return list(self._requests)

    def clear_requests(self) -> None:
        with self._lock:
            self._requests.clear()

    # -- lifecycle -------------------------------------------------------

    def stop(self) -> None:
        """Stop listening and close every open connection; safe to call twice."""
        with self._lock:
            self._stopped = True
            open_conns = list(self._connections)
        for srv in self._servers:
            srv.stopping = True
        # shutdown() waits for serve_forever's next poll; stop all listeners at
        # once instead of one after another.
        stoppers = [threading.Thread(target=srv.shutdown, daemon=True)
                    for srv in self._servers]
        for stopper in stoppers:
            stopper.start()
        for stopper in stoppers:
            stopper.join(timeout=5)
        for srv in self._servers:
            srv.server_close()
        # Keep-alive connections outlive the listeners: without this a browser's
        # pooled connection would still be served by a "stopped" server.
        for conn in open_conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
        for thread in self._threads:
            thread.join(timeout=5)

    def __enter__(self) -> "FixtureServer":
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()


def start_fixture_server(host: str = "127.0.0.1", port: int = 0,
                         alt_port: int = 0) -> FixtureServer:
    """Start the fixture site; ``port=0`` / ``alt_port=0`` pick free ports."""
    return FixtureServer(host, port, alt_port)
