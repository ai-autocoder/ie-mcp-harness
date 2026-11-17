"""Two-origin HTTP server for the legacy fixture site.

The fixture site reproduces legacy IE-only patterns (framesets, old document
modes, showModalDialog, ActiveX XMLHTTP, form posts, windows-1252 pages) so the
harness can be tested without any real application. It is served from two
listeners on the same host but different ports, which gives the pages a second
origin for cross-origin frame tests.

Every response is marked ``Cache-Control: no-store`` and carries no
validators, so nothing the tests load lands in the per-user WinINet cache that
the user's own IE-mode browsing shares.
"""

from __future__ import annotations

import html
import http.server
import mimetypes
import socket
import threading
import time
import urllib.parse
from pathlib import Path

SITE_DIR = Path(__file__).resolve().parent / "site"

ALT_BASE_PLACEHOLDER = b"{{ALT_BASE}}"
MAX_BODY_BYTES = 10 * 1024 * 1024
MAX_SLOW_MS = 60_000

LOOKUP_ITEMS = (("A1", "Alpha"), ("B2", "Beta"), ("C3", "Gamma"))

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

    _cache_header_sent = False

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

    def _slow(self, ms):
        try:
            delay = max(0, min(int(ms), MAX_SLOW_MS))
        except ValueError:
            self._send_text(400, "ms must be an integer")
            return
        time.sleep(delay / 1000.0)
        self._send_text(200, "slow ok")


class _ExclusiveHTTPServer(http.server.ThreadingHTTPServer):
    # HTTPServer sets allow_reuse_address, which on Windows maps to
    # SO_REUSEADDR and lets a second socket bind a port that is already in
    # use: two "origins" could silently end up on one port, or the fixture
    # server could share a port with another program. Bind exclusively.
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class FixtureServer:
    """The fixture site on two ports of one host (two origins).

    ``base_url`` and ``alt_base_url`` serve the same content. Use it as a
    context manager, or call ``stop()``.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0, alt_port: int = 0):
        if port and port == alt_port:
            raise ValueError("port and alt_port must differ (two origins)")
        handler = type("FixtureHandler", (_FixtureHandler,), {})
        self._servers = []
        try:
            for p in (port, alt_port):
                self._servers.append(_ExclusiveHTTPServer((host, p), handler))
        except BaseException:
            for srv in self._servers:
                srv.server_close()
            raise
        self.base_url = _url(host, self._servers[0].server_address[1])
        self.alt_base_url = _url(host, self._servers[1].server_address[1])
        handler.alt_base_url = self.alt_base_url
        self._threads = [
            threading.Thread(target=srv.serve_forever, name="fixture-server",
                             daemon=True)
            for srv in self._servers
        ]
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        for srv in self._servers:
            srv.shutdown()
            srv.server_close()
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
