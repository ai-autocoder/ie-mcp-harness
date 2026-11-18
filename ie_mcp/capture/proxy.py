"""Streaming recording proxy for the IE-mode browser.

Only the browser process uses it (IEDriver's per-process proxy capability); the system and
WinINet proxy settings are never touched, and the proxy makes no connection of its own: it
forwards what the browser asks for, directly or to a configured upstream proxy.

Every exchange goes into the ``CaptureStore``. Page documents (a request with ``Accept:
text/html`` answered by a full HTML document) additionally get a tiny loader ``<script>``
inserted, so the page can be instrumented without changing how it behaves; the rules for where
the loader may go are in ``find_insertion_point`` and in CLAUDE.md ("Injection rules").
Everything else, fragments, plain text and XHR answers included, is passed through byte for byte.
"""
import http.client
import http.server
import re
import select
import socket
import socketserver
import sys
import threading
import time
import urllib.parse

from ie_mcp.capture.redact import parse_content_type
from ie_mcp.capture.store import NetworkEntry

LOADER_PREFIX = "/__iemcp__/"
MAX_SCAN = 65536  # the most the insertion scan ever buffers
IO_BLOCK = 16384
PRESCAN_BYTES = 2048  # IE's charset prescan reads about this much of a document
SEEN_LIMIT = 1000
CHARSET_LIMIT = 1000
CONNECT_TIMEOUT = 30
# Legacy pages can take minutes to answer (reports): never time an exchange out early.
UPSTREAM_TIMEOUT = 600
CLIENT_IDLE_TIMEOUT = 600
REQUEST_BUFFER_LIMIT = 1 << 20  # request bodies up to this are buffered (and can be replayed)
CHUNKED_REQUEST_LIMIT = 32 << 20


class _Sentinel:
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return self.name


NEVER = _Sentinel("NEVER")
NEED_MORE = _Sentinel("NEED_MORE")


# ---------------------------------------------------------------------------------------
# Where the loader goes
# ---------------------------------------------------------------------------------------

_WS = b" \t\r\n\f"
_NAME_END = _WS + b"/>"
_UTF8_BOM = b"\xef\xbb\xbf"
_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")  # also the start of a UTF-32 LE BOM
_TITLE_END = re.compile(rb"</title", re.IGNORECASE)
_HOST_OK = re.compile(r"^[A-Za-z0-9._:-]+$")


class _Short(Exception):
    """The buffer ends before the answer is known."""


def _skip(buf, pos):
    """Index of the first byte after whitespace and comments starting at ``pos``."""
    n = len(buf)
    while True:
        while pos < n and buf[pos] in _WS:
            pos += 1
        if pos >= n or buf[pos] != 0x3C:
            return pos
        if buf.startswith(b"<!--", pos):
            end = buf.find(b"-->", pos + 4)
            if end < 0:
                raise _Short
            pos = end + 3
            continue
        if n - pos < 4 and b"<!--".startswith(buf[pos:]):
            raise _Short  # "<", "<!" or "<!-" at the very end: could still be a comment
        return pos


def _tag_end(buf, i):
    """Index after the ``>`` that ends the tag whose name ended at ``i``.

    A ``>`` inside a quoted attribute value (a quote right after ``=``) does not end the tag.
    """
    n = len(buf)
    while i < n:
        c = buf[i]
        if c == 0x3E:
            return i + 1
        if c == 0x3D:
            i += 1
            while i < n and buf[i] in _WS:
                i += 1
            if i >= n:
                raise _Short
            if buf[i] in b"\"'":
                close = buf.find(buf[i:i + 1], i + 1)
                if close < 0:
                    raise _Short
                i = close + 1
            continue
        i += 1
    raise _Short


def _read_tag(buf, pos):
    """Parse the tag whose ``<`` is at ``pos``: ``(name, end)``.

    ``name`` is lower case, ``/``-prefixed for an end tag, and ``!doctype`` for a doctype; it is
    ``""`` when the ``<`` is plain text (not followed by a letter, ``/``, ``!`` or ``?``). A
    name counts only once whitespace, ``/`` or ``>`` follows it (``<head`` is not ``<header``),
    and the tag only once its ``>`` has arrived.
    """
    n = len(buf)
    i = pos + 1
    if i >= n:
        raise _Short
    first = buf[i]
    if first == 0x2F:
        i += 1
        if i >= n:
            raise _Short
        first = buf[i]
    if not (0x41 <= first <= 0x5A or 0x61 <= first <= 0x7A or first in b"!?"):
        return "", pos + 1
    while i < n and buf[i] not in _NAME_END:
        i += 1
    if i >= n:
        raise _Short
    name = buf[pos + 1:i].decode("latin-1").lower()
    return name, _tag_end(buf, i)


def _peek_tag(buf, pos):
    """The tag after whitespace and comments: ``(name, end)``; ``("", pos)`` for text."""
    pos = _skip(buf, pos)
    if pos >= len(buf):
        raise _Short
    if buf[pos] != 0x3C:
        return "", pos
    return _read_tag(buf, pos)


def _after_title(buf, pos):
    """Index after the ``</title>`` that closes the title whose start tag ended at ``pos``."""
    n = len(buf)
    while True:
        match = _TITLE_END.search(buf, pos)
        if match is None:
            raise _Short
        end = match.end()
        if end >= n:
            raise _Short
        if buf[end] in _NAME_END:
            return _tag_end(buf, end)
        pos = end


def _scan(buf, state):
    n = len(buf)
    if buf[:2] in _UTF16_BOMS:
        return NEVER  # a UTF-16 body: byte-wise insertion would corrupt it
    if n < 2 and buf[:1] in (b"\xff", b"\xfe"):
        raise _Short
    pos = 0
    if buf.startswith(_UTF8_BOM):
        pos = 3  # never insert before a byte-order mark
    elif n < 3 and _UTF8_BOM.startswith(buf):
        raise _Short
    pos = _skip(buf, pos)
    if pos >= n:
        raise _Short
    if buf[pos] != 0x3C:
        return NEVER  # plain text
    name, end = _read_tag(buf, pos)
    if name == "body":
        return pos
    if name not in ("!doctype", "html", "head"):
        return NEVER  # a fragment (or a frameset first): never touched
    anchor = end
    state["fallback"] = anchor
    have_head = name == "head"
    if name == "!doctype":
        tag, tag_end = _peek_tag(buf, anchor)
        if tag in ("html", "head"):
            name, anchor, have_head = tag, tag_end, tag == "head"
            state["fallback"] = anchor
    if name == "html":
        tag, tag_end = _peek_tag(buf, anchor)
        if tag == "head":
            anchor, have_head = tag_end, True
            state["fallback"] = anchor
    # IE honours an X-UA-Compatible meta only before the first script, and reads a charset meta
    # only near the start: skip every leading meta, title and base so the loader never comes
    # before them. An offset is final only once the tag after the run has fully arrived.
    pos = anchor
    while True:
        state["fallback"] = pos
        pos = _skip(buf, pos)
        state["fallback"] = pos
        if pos >= n:
            raise _Short
        if buf[pos] != 0x3C:
            return pos
        name, end = _read_tag(buf, pos)
        if name in ("meta", "base"):
            pos = end
        elif name == "title":
            pos = _after_title(buf, end)
        elif name == "frameset" and not have_head:
            return NEVER  # a head-less frameset is instrumented after load instead
        else:
            return pos


def find_insertion_point(buf, eof):
    """Where in a document body the loader goes: an offset, ``NEVER`` or ``NEED_MORE``.

    ``buf`` is everything received so far (at most 64 KB); ``eof`` says the body is complete.
    """
    state = {"fallback": NEVER}
    try:
        return _scan(buf, state)
    except _Short:
        if len(buf) >= MAX_SCAN:
            return NEVER  # nothing decided within 64 KB
        return state["fallback"] if eof else NEED_MORE


def loader_tag(doc_url):
    """The loader ``<script>`` for a document at ``doc_url``.

    The src is absolute (the document's own host and port), so a ``<base href>`` cannot
    redirect it. ``i.js`` removes its own element when it runs.
    """
    parts = urllib.parse.urlsplit(doc_url)
    host = parts.hostname
    if not host or not _HOST_OK.match(host):
        raise ValueError("no usable host in %r" % (doc_url,))
    if ":" in host:
        host = "[" + host + "]"
    port = parts.port
    authority = host + (":%d" % port if port else "")
    return ('<script type="text/javascript" src="http://%s%si.js"></script>'
            % (authority, LOADER_PREFIX)).encode("ascii")


def is_document_request(method, headers):
    """True for a GET or POST that accepts ``text/html`` (spike D10: documents do, XHR sends */*)."""
    if method.upper() not in ("GET", "POST"):
        return False
    accept = ",".join(v for k, v in headers.items() if k.lower() == "accept")
    return "text/html" in accept.lower()


# ---------------------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------------------

HOP_BY_HOP = frozenset((
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection",
    "te", "trailer", "trailers", "transfer-encoding", "upgrade"))
_CONDITIONAL = frozenset(("if-modified-since", "if-none-match"))
_META_CHARSET = re.compile(rb"<meta\b[^>]*?charset\s*=\s*[\"']?\s*([A-Za-z0-9_.:-]+)", re.IGNORECASE)


def _hget(headers, name):
    for key, value in headers:
        if key.lower() == name:
            return value
    return ""


def _listed(headers):
    """Header names a ``Connection`` header declares hop-by-hop."""
    names = set()
    for key, value in headers:
        if key.lower() == "connection":
            names.update(t.strip().lower() for t in value.split(",") if t.strip())
    return names


def _request_headers(headers, upstream, no_cache, identity):
    """The browser's headers minus the hop-by-hop ones.

    ``Proxy-Authorization`` goes on only to a configured upstream proxy. ``Expect`` is dropped
    because this proxy has already answered ``100 Continue`` itself.
    """
    items = list(headers)
    listed = _listed(items)
    out = []
    for name, value in items:
        low = name.lower()
        if low == "proxy-authorization":
            if upstream:
                out.append((name, value))
            continue
        if low in HOP_BY_HOP or low in listed or low == "expect":
            continue
        if no_cache and low in _CONDITIONAL:
            continue
        if identity and low == "accept-encoding":
            continue
        out.append((name, value))
    if identity:
        out.append(("Accept-Encoding", "identity"))
    return out


def _is_encoded(headers):
    return _hget(headers, "content-encoding").strip().lower() not in ("", "identity")


def _scan_charset(prefix):
    match = _META_CHARSET.search(bytes(prefix))
    return match.group(1).decode("latin-1").lower() if match else None


def _split_hostport(target):
    host, sep, port = target.rpartition(":")
    if not sep or not host or not port.isdigit():
        raise ValueError("bad CONNECT target %r" % (target,))
    return host.strip("[]"), int(port)


class _ClientGone(Exception):
    """The browser closed its connection (it cancels loads all the time)."""


class _Refused(Exception):
    def __init__(self, status, reason, text):
        super().__init__(text)
        self.status, self.reason, self.text = status, reason, text


class _Capture:
    """Copies up to ``cap`` bytes of a body, and notes whether it had to cut."""

    def __init__(self, cap):
        self.cap = cap
        self.data = bytearray()
        self.truncated = False

    def add(self, data):
        room = self.cap - len(self.data)
        if room > 0:
            self.data += data[:room]
        if len(data) > max(room, 0):
            self.truncated = True


# ---------------------------------------------------------------------------------------
# The proxy
# ---------------------------------------------------------------------------------------

class _Server(socketserver.ThreadingTCPServer):
    # Bind exclusively: SO_REUSEADDR on Windows would let a second socket (or another program)
    # share the port, and the browser's traffic could then go to the wrong listener.
    allow_reuse_address = False
    daemon_threads = True
    request_queue_size = 128

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def handle_error(self, request, client_address):
        # Browsers cancel loads and drop idle connections; that is not a server error.
        if isinstance(sys.exc_info()[1], OSError):
            return
        super().handle_error(request, client_address)


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = CLIENT_IDLE_TIMEOUT
    # Without TCP_NODELAY a small write (a progressive page's head) can sit behind Nagle's
    # algorithm and a delayed ACK for 200 ms on Windows loopback.
    disable_nagle_algorithm = True
    proxy = None  # the owning RecordingProxy, set on a per-proxy subclass

    def __getattr__(self, name):
        # Every method (GET, POST, PROPFIND...) goes through one dispatcher.
        if name.startswith("do_"):
            return self._dispatch
        raise AttributeError(name)

    def log_message(self, format, *args):  # noqa: A002 - signature from the base class
        pass

    # -- lifecycle -------------------------------------------------------------------
    def setup(self):
        super().setup()
        self._upstreams = {}      # key -> http.client.HTTPConnection, for this client connection
        self._extra_socks = []    # tunnel sockets, shut down on stop()
        self._proxy_sock = None   # an upstream-proxy socket kept across a CONNECT 407 challenge
        self._head_sent = False
        self._accepted = self.proxy._register(self)

    def handle(self):
        if self._accepted:
            super().handle()

    def finish(self):
        try:
            for conn in list(self._upstreams.values()):
                conn.close()
            self._upstreams.clear()
            if self._proxy_sock is not None:
                self._proxy_sock.close()
                self._proxy_sock = None
            for sock in list(self._extra_socks):  # a tunnel socket nothing else closed
                self._discard(sock)
            super().finish()
        finally:
            self.proxy._unregister(self)

    def _discard(self, sock):
        """Close a tunnel socket and forget it."""
        if sock in self._extra_socks:
            self._extra_socks.remove(sock)
        try:
            sock.close()
        except OSError:
            pass

    def abort(self):
        """Called by stop(): wake every blocked read or write of this connection."""
        socks = [self.connection] + list(self._extra_socks)
        socks += [c.sock for c in list(self._upstreams.values())]
        if self._proxy_sock is not None:
            socks.append(self._proxy_sock)
        for sock in socks:
            if sock is None:
                continue
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            # Windows quirk: a thread blocked in recv/select on a socket with a timeout is NOT
            # woken by shutdown(), and close() is deferred while a makefile() file object still
            # references the socket. Closing the OS handle itself is what wakes it (with
            # WSAENOTSOCK); the object's own close() later is then a no-op.
            try:
                socket.socket._real_close(sock)
            except (AttributeError, OSError):
                pass

    # -- plumbing --------------------------------------------------------------------
    def _write(self, data):
        try:
            self.wfile.write(data)
        except OSError as exc:
            raise _ClientGone(str(exc)) from exc

    def _plain(self, status, reason, text, extra=(), close=False):
        body = text.encode("utf-8")
        lines = ["HTTP/1.1 %d %s" % (status, reason), "Content-Type: text/plain; charset=utf-8",
                 "Content-Length: %d" % len(body), "Cache-Control: no-store"]
        lines += ["%s: %s" % pair for pair in extra]
        if close or self.close_connection:
            lines.append("Connection: close")
            self.close_connection = True
        self._head_sent = True
        self._write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body)

    def _connection(self, key, host, port):
        """This client connection's own upstream connection for ``key``.

        Kept for the client connection's lifetime, so connection-based Windows authentication
        (NTLM/Negotiate), which authenticates the TCP connection, keeps working.
        """
        conn = self._upstreams.get(key)
        reused = conn is not None and conn.sock is not None
        if conn is None:
            conn = http.client.HTTPConnection(host, port, timeout=CONNECT_TIMEOUT)
            self._upstreams[key] = conn
        if conn.sock is None:
            conn.connect()
            conn.sock.settimeout(UPSTREAM_TIMEOUT)
        return conn, reused

    def _drop(self, key):
        conn = self._upstreams.pop(key, None)
        if conn is not None:
            conn.close()

    # -- dispatch --------------------------------------------------------------------
    def _dispatch(self):
        try:
            if self.command == "CONNECT":
                self._tunnel()
            else:
                self._request()
        except _ClientGone:
            self.close_connection = True
        except Exception as exc:  # a bug must not leave a half-written response on a live connection
            self.close_connection = True
            self.proxy._internal_error(exc)

    @staticmethod
    def _target(split):
        try:
            if split is None or split.scheme.lower() != "http":
                raise _Refused(400, "Bad Request", "ie-mcp proxy: absolute http:// URLs only")
            host, port = split.hostname, split.port or 80
        except ValueError:
            raise _Refused(400, "Bad Request", "ie-mcp proxy: bad URL") from None
        if not host:
            raise _Refused(400, "Bad Request", "ie-mcp proxy: no host in the URL")
        return host, port

    def _request(self):
        proxy = self.proxy
        method = self.command
        target = self.path
        absolute = "://" in target.split("?", 1)[0]
        split = urllib.parse.urlsplit(target) if absolute else None
        path = split.path if split else target.split("?", 1)[0]
        if path.startswith(LOADER_PREFIX):
            return self._loader(method)  # answered here, on any host: never forwarded or recorded
        try:
            host, port = self._target(split)
        except _Refused as exc:
            return self._safe_plain(exc.status, exc.reason, exc.text)
        entry = NetworkEntry(started=time.time(), method=method, url=target,
                             req_headers=list(self.headers.items()), source="proxy")
        entry.referer_charset = proxy._referer_charset(self.headers.get("Referer"))
        proxy._note_request(target)
        began = time.monotonic()
        self._head_sent = False
        try:
            self._exchange(entry, split, host, port)
        except _ClientGone as exc:
            self.close_connection = True
            entry.error = entry.error or "client closed the connection: %s" % exc
        except _Refused as exc:
            self.close_connection = True
            entry.status, entry.error = exc.status, exc.text
            self._safe_plain(exc.status, exc.reason, exc.text)
        except Exception as exc:
            self.close_connection = True
            entry.error = "%s: %s" % (type(exc).__name__, exc)
            if not isinstance(exc, (OSError, http.client.HTTPException, ValueError)):
                proxy._internal_error(exc)
            if not self._head_sent:
                entry.status = 502
                self._safe_plain(502, "Bad Gateway", "ie-mcp proxy: %s" % entry.error)
        finally:
            entry.duration_ms = int((time.monotonic() - began) * 1000)
            proxy.store.add(entry)

    def _safe_plain(self, status, reason, text):
        try:
            self._plain(status, reason, text, close=True)
        except _ClientGone:
            pass

    def _loader(self, method):
        js = self.proxy.instrument_js
        if method not in ("GET", "HEAD"):
            return self._plain(405, "Method Not Allowed", "GET only", [("Allow", "GET, HEAD")],
                               close=True)
        if js is None:
            return self._plain(404, "Not Found", "instrumentation is off")
        lines = ["HTTP/1.1 200 OK", "Content-Type: application/javascript",
                 "Content-Length: %d" % len(js), "Cache-Control: no-store"]
        if self.close_connection:
            lines.append("Connection: close")
        self._write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
                    + (b"" if method == "HEAD" else js))

    # -- request body ----------------------------------------------------------------
    def _read_chunked_request(self):
        data = bytearray()
        while True:
            line = self.rfile.readline(65537)
            if not line:
                raise _ClientGone("request body cut short")
            try:
                size = int(line.split(b";", 1)[0].strip() or b"0", 16)
            except ValueError:
                raise _Refused(400, "Bad Request", "bad chunk size") from None
            if size == 0:
                while self.rfile.readline(65537) not in (b"\r\n", b"\n", b""):
                    pass  # trailers
                return bytes(data)
            if len(data) + size > CHUNKED_REQUEST_LIMIT:
                raise _Refused(413, "Payload Too Large", "chunked request body too large")
            chunk = self.rfile.read(size)
            if len(chunk) != size:
                raise _ClientGone("request body cut short")
            self.rfile.readline(65537)  # the CRLF after the chunk
            data += chunk

    def _request_body(self):
        """``(data, stream_length)``: the body when it is buffered, else its length to stream."""
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            return self._read_chunked_request(), None
        raw = self.headers.get("Content-Length")
        if raw is None:
            return b"", None
        try:
            length = int(raw)
            if length < 0:
                raise ValueError
        except ValueError:
            raise _Refused(400, "Bad Request", "bad Content-Length") from None
        if length > REQUEST_BUFFER_LIMIT:
            return None, length
        data = self.rfile.read(length) if length else b""
        if len(data) != length:
            raise _ClientGone("request body cut short")
        return data, None

    # -- the exchange ----------------------------------------------------------------
    def _exchange(self, entry, split, host, port):
        proxy = self.proxy
        method = self.command
        upstream = proxy.upstream_proxy
        if not upstream and port == proxy.port and host in (proxy.host, "localhost", "127.0.0.1"):
            raise _Refused(508, "Loop Detected", "ie-mcp proxy: request to the proxy itself")
        is_doc = proxy.instrument_js is not None and is_document_request(method, self.headers)
        data, stream_length = self._request_body()
        capture = _Capture(proxy.store.max_body)
        if data is not None:
            capture.add(data)
        headers = _request_headers(self.headers.items(), bool(upstream), proxy.no_cache, is_doc)
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            headers = [h for h in headers if h[0].lower() != "content-length"]
            headers.append(("Content-Length", str(len(data))))
        netloc = split.netloc.rpartition("@")[2]
        headers = [h for h in headers if h[0].lower() != "host"]
        headers.insert(0, ("Host", netloc))
        if upstream:
            request_target = urllib.parse.urldefrag(self.path)[0]
            key, connect_to = ("proxy",), upstream
        else:
            request_target = (split.path or "/") + ("?" + split.query if split.query else "")
            key, connect_to = (host, port), (host, port)
        try:
            resp = self._send(key, connect_to, method, request_target, headers, data,
                              stream_length, capture)
        finally:
            entry.req_body = bytes(capture.data)
            entry.truncated = entry.truncated or capture.truncated
        self._respond(entry, key, resp, is_doc)

    def _send(self, key, connect_to, method, target, headers, data, stream_length, capture):
        """Send the request upstream and return its response (retrying one stale connection)."""
        for attempt in (0, 1):
            conn, reused = self._connection(key, *connect_to)
            try:
                conn.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
                for name, value in headers:
                    conn.putheader(name, value)
                conn.endheaders(data or None)
                if data is None:
                    remaining = stream_length
                    while remaining:
                        try:
                            block = self.rfile.read(min(65536, remaining))
                        except OSError as exc:
                            raise _ClientGone(str(exc)) from exc
                        if not block:
                            raise _ClientGone("request body cut short")
                        capture.add(block)
                        conn.send(block)
                        remaining -= len(block)
                return conn.getresponse()
            except (ConnectionError, http.client.BadStatusLine):
                # A kept-alive connection the origin closed while idle: try once more on a
                # fresh one, as browsers do, but only if the whole body can be replayed.
                self._drop(key)
                if attempt == 0 and reused and data is not None:
                    continue
                raise
            except BaseException:
                self._drop(key)
                raise

    # -- the response ----------------------------------------------------------------
    def _respond(self, entry, key, resp, is_doc):
        proxy = self.proxy
        method = self.command
        status = resp.status
        up_headers = resp.getheaders()
        entry.status = status
        entry.resp_headers = list(up_headers)
        content_type = _hget(up_headers, "content-type")
        media, params = parse_content_type(content_type)
        is_html = media == "text/html"
        no_body = method == "HEAD" or status in (204, 304) or 100 <= status < 200
        chunked_up = bool(resp.chunked)
        length_up = None if chunked_up else resp.length
        capture = _Capture(proxy.store.max_body)
        charset = (params.get("charset") or "").lower() or None
        prefix = bytearray() if is_html and not charset else None
        if is_html and charset:
            proxy._set_charset(entry.url, charset)
        done = False
        try:
            if no_body:
                self._write(self._head(status, resp.reason, up_headers, None, None, False))
                resp.read()
                done = True
                return
            injectable = (
                is_doc and status == 200 and is_html
                and not _hget(up_headers, "content-disposition").strip().lower().startswith("attachment")
                and not _is_encoded(up_headers))

            def take(chunk):
                capture.add(chunk)
                nonlocal prefix
                if prefix is not None:
                    prefix += chunk[:PRESCAN_BYTES - len(prefix)]
                    if len(prefix) >= PRESCAN_BYTES:
                        proxy._set_charset(entry.url, _scan_charset(prefix))
                        prefix = None

            first = b""
            insert = None
            eof = False
            if injectable:
                # Hold the head back only while the insertion point is still undecided.
                buf = bytearray()
                decision = NEED_MORE
                while decision is NEED_MORE:
                    chunk = self._read(resp, min(IO_BLOCK, MAX_SCAN - len(buf)))
                    if chunk:
                        take(chunk)
                    else:
                        eof = True
                    buf += chunk
                    decision = find_insertion_point(bytes(buf), eof)
                if decision is NEVER:
                    first = bytes(buf)
                else:
                    try:
                        tag = loader_tag(entry.url)
                    except ValueError:  # no usable host: leave the document alone
                        tag = None
                    if tag is None:
                        first = bytes(buf)
                    else:
                        insert = tag
                        first = bytes(buf[:decision]) + tag + bytes(buf[decision:])
            if chunked_up or length_up is None:
                framing = "chunked" if self.request_version != "HTTP/1.0" else "close"
            else:
                framing = "length"
            out_length = None
            if framing == "length":
                out_length = length_up + (len(insert) if insert else 0)
            if framing == "close":
                self.close_connection = True
            head = self._head(status, resp.reason, up_headers, framing, out_length,
                              insert is not None)
            self._write(head + (self._frame(first, framing) if first else b""))
            while not eof:
                chunk = self._read(resp, IO_BLOCK)
                if not chunk:
                    break
                take(chunk)
                self._write(self._frame(chunk, framing))
            if framing == "chunked":
                self._write(b"0\r\n\r\n")
            done = True
        finally:
            entry.resp_body = bytes(capture.data)
            entry.truncated = entry.truncated or capture.truncated
            if prefix is not None and is_html:
                proxy._set_charset(entry.url, _scan_charset(prefix))
            if not done:
                self._drop(key)  # a half-read response leaves the connection unusable

    @staticmethod
    def _frame(data, framing):
        if framing == "chunked":
            return b"%x\r\n%s\r\n" % (len(data), data)
        return data

    @staticmethod
    def _read(resp, size):
        """The next piece of the body: whatever has arrived (``read1``), never a full block.

        Raises when the origin closes a response that is shorter than it announced:
        ``read1`` alone would just return empty bytes, and the browser must not be told the
        truncated body is complete.
        """
        chunk = resp.read1(size)
        if not chunk and resp.length:
            raise http.client.IncompleteRead(b"", resp.length)
        return chunk

    def _head(self, status, reason, up_headers, framing, length, modified):
        """The response head to send: the origin's headers minus the hop-by-hop ones.

        A modified (injected) response also loses its validators and is made uncacheable, so
        the injected copy never lands in the per-user WinINet cache the user's own IE-mode
        browsing shares.
        """
        listed = _listed(up_headers)
        out = []
        cache_done = False
        for name, value in up_headers:
            low = name.lower()
            if (low in HOP_BY_HOP and low != "proxy-authenticate") or low in listed:
                continue
            if low == "content-length":
                if framing is None:
                    out.append((name, value))  # HEAD / 304: the origin's value describes the entity
                elif framing == "length":
                    out.append((name, str(length)))
                continue
            if modified and low in ("etag", "last-modified", "content-md5"):
                continue
            if modified and low == "cache-control":
                if not cache_done:
                    out.append((name, "no-store"))
                    cache_done = True
                continue
            out.append((name, value))
        if modified and not cache_done:
            out.append(("Cache-Control", "no-store"))
        if framing == "chunked":
            out.append(("Transfer-Encoding", "chunked"))
        if self.close_connection:
            out.append(("Connection", "close"))
        text = "HTTP/1.1 %d %s\r\n" % (status, reason or "")
        text += "".join("%s: %s\r\n" % pair for pair in out) + "\r\n"
        self._head_sent = True
        return text.encode("latin-1", "replace")

    # -- CONNECT ---------------------------------------------------------------------
    def _tunnel(self):
        proxy = self.proxy
        target = self.path
        entry = NetworkEntry(started=time.time(), method="CONNECT", url=target, tunnel=True,
                             req_headers=list(self.headers.items()), source="proxy")
        proxy._note_request(target)
        began = time.monotonic()
        self._head_sent = False
        sock, initial = None, b""
        try:
            sock, initial = self._open_tunnel(entry, target)
        except _ClientGone:
            self.close_connection = True
            entry.error = "client closed the connection"
        except (OSError, ValueError, IndexError) as exc:
            self.close_connection = True
            entry.error, entry.status = "%s: %s" % (type(exc).__name__, exc), 502
            self._safe_plain(502, "Bad Gateway", "ie-mcp proxy: %s" % entry.error)
        # Recorded as soon as the tunnel is up (it may stay open for the whole session), or
        # when it could not be opened.
        entry.duration_ms = int((time.monotonic() - began) * 1000)
        proxy.store.add(entry)
        if sock is None:
            return
        self.close_connection = True
        try:
            self._relay(sock, initial)
        except _ClientGone:
            pass
        finally:
            self._discard(sock)

    def _open_tunnel(self, entry, target):
        """Connect to the target (or ask the upstream proxy to) and answer the browser.

        Returns ``(socket, bytes already read after the upstream's head)``, or ``(None, b"")``
        when the upstream proxy refused and its answer was relayed instead.
        """
        host, port = _split_hostport(target)
        upstream = self.proxy.upstream_proxy
        sock = self._proxy_sock if upstream else None
        self._proxy_sock = None
        try:
            if sock is None:
                sock = socket.create_connection(upstream or (host, port), timeout=CONNECT_TIMEOUT)
                sock.settimeout(UPSTREAM_TIMEOUT)
            self._extra_socks.append(sock)
            if not upstream:
                entry.status = 200
                self._write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                return sock, b""
            lines = ["CONNECT %s HTTP/1.1" % target, "Host: %s" % target]
            auth = self.headers.get("Proxy-Authorization")
            if auth:
                lines.append("Proxy-Authorization: %s" % auth)
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
            head, rest = self._read_head(sock)
            lines = head.decode("latin-1").split("\r\n")
            entry.status = int(lines[0].split(" ", 2)[1])
            entry.resp_headers = [tuple(p.strip() for p in line.split(":", 1))
                                  for line in lines[1:] if ":" in line]
            if entry.status != 200:
                # Typically a 407 challenge. Relay it whole and keep the upstream socket: a
                # connection-based scheme (NTLM) continues on the same connection.
                self._relay_challenge(sock, head, rest, entry)
                return None, b""
            self._write(head)
            return sock, rest
        except BaseException:
            # _ClientGone, a refused connection, a malformed answer: nobody else holds the socket.
            if sock is not None:
                self._discard(sock)
            raise

    @staticmethod
    def _read_head(sock):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                raise OSError("the upstream proxy closed the connection")
            data += chunk
            if len(data) > MAX_SCAN:
                raise OSError("the upstream proxy's response head is too large")
        cut = data.index(b"\r\n\r\n") + 4
        return data[:cut], data[cut:]

    def _relay_challenge(self, sock, head, rest, entry):
        headers = entry.resp_headers
        raw = _hget(headers, "content-length")
        keep = raw.isdigit() and "close" not in _hget(headers, "connection").lower()
        body = rest
        if keep:
            while len(body) < int(raw):
                chunk = sock.recv(65536)
                if not chunk:
                    keep = False
                    break
                body += chunk
        self._write(head + body)
        if keep:
            self._proxy_sock = sock
            self._extra_socks.remove(sock)
        else:
            self.close_connection = True
            self._discard(sock)

    def _relay(self, upstream, initial):
        client = self.connection
        if initial:
            self._write(initial)
        peers = {client: upstream, upstream: client}
        while peers and not self.proxy._stopping:
            try:
                readable, _, _ = select.select(list(peers), [], [], 0.5)
            except (OSError, ValueError):
                return
            for sock in readable:
                if sock not in peers:
                    continue
                try:
                    data = self.rfile.read1(65536) if sock is client else sock.recv(65536)
                except OSError:
                    return
                other = peers[sock]
                if not data:
                    # A half-close: pass the FIN on and keep relaying the other direction.
                    del peers[sock]
                    try:
                        other.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    continue
                try:
                    other.sendall(data)
                except OSError:
                    return


class RecordingProxy:
    """A recording forward proxy on ``host:port`` (``port=0`` picks a free one).

    ``seen`` lists the request URLs and CONNECT targets in order (the most recent 1000);
    ``first_request`` is set by the first of them; ``doc_charsets`` maps the URL of each
    ``text/html`` response to its charset (the 1000 most recent). A request path starting with
    ``/__iemcp__/`` is answered by the proxy itself on any host (``instrument_js``, or a 404
    when injection is off) and is neither forwarded nor recorded.
    """

    def __init__(self, store, host="127.0.0.1", port=0, instrument_js=None, upstream_proxy=None,
                 no_cache=False):
        self.store = store
        self.host = host
        self.port = port
        self.instrument_js = instrument_js
        self.upstream_proxy = tuple(upstream_proxy) if upstream_proxy else None
        self.no_cache = no_cache
        self.seen = []
        self.first_request = threading.Event()
        self.doc_charsets = {}
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._handlers = set()
        self.internal_errors = []  # unexpected exceptions in a handler (bugs), for tests and ie_status
        self._server = None
        self._thread = None
        self._stopping = False

    # -- lifecycle -------------------------------------------------------------------
    def start(self):
        with self._lock:
            if self._server is not None:
                return self.port
            handler = type("ProxyHandler", (_Handler,), {"proxy": self})
            self._server = _Server((self.host, self.port), handler)
            self.port = self._server.server_address[1]
            self._stopping = False
            # A short poll interval keeps stop() fast: shutdown() waits for the next poll.
            self._thread = threading.Thread(
                target=self._server.serve_forever, kwargs={"poll_interval": 0.05},
                name="ie-mcp-proxy", daemon=True)
            self._thread.start()
            return self.port

    def stop(self):
        with self._lock:
            server, thread = self._server, self._thread
            if server is None:
                return
            self._server = self._thread = None
            self._stopping = True
            handlers = list(self._handlers)
        server.shutdown()
        server.server_close()
        for handler in handlers:
            handler.abort()
        thread.join(5)
        with self._cond:
            self._cond.wait_for(lambda: not self._handlers, timeout=5)

    # -- handler bookkeeping ---------------------------------------------------------
    def _register(self, handler):
        """False once stop() has begun: such a connection is not served."""
        with self._lock:
            if self._stopping:
                return False
            self._handlers.add(handler)
            return True

    def _unregister(self, handler):
        with self._cond:
            self._handlers.discard(handler)
            self._cond.notify_all()

    def _internal_error(self, exc):
        with self._lock:
            if len(self.internal_errors) < 20:
                self.internal_errors.append("%s: %s" % (type(exc).__name__, exc))

    def _note_request(self, url):
        with self._lock:
            self.seen.append(url)
            if len(self.seen) > SEEN_LIMIT:
                del self.seen[:len(self.seen) - SEEN_LIMIT]
        self.first_request.set()

    def _set_charset(self, url, charset):
        if not charset:
            return
        with self._lock:
            self.doc_charsets.pop(url, None)
            self.doc_charsets[url] = charset
            while len(self.doc_charsets) > CHARSET_LIMIT:
                del self.doc_charsets[next(iter(self.doc_charsets))]

    def _referer_charset(self, referer):
        if not referer:
            return None
        with self._lock:
            return self.doc_charsets.get(referer.split("#", 1)[0])
