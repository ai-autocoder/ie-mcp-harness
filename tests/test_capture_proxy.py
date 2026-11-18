"""Unit tests for the recording proxy (no browser).

Every request goes to the proxy in absolute form through ``http.client`` (never urllib), so the
registry ProxyOverride list and NO_PROXY cannot bypass it. Origins are the fixture server and a
small scripted stub that answers with exact, hand-written headers.
"""
import collections
import gc
import gzip
import http.client
import http.server
import socket
import threading
import time
import unittest
import urllib.parse
import warnings

from ie_mcp.capture.proxy import RecordingProxy, find_insertion_point, loader_tag
from ie_mcp.capture.redact import Redactor
from ie_mcp.capture.store import CaptureStore
from ie_mcp_fixtures.server import BIG_PATTERN, start_fixture_server

FORM = {"Content-Type": "application/x-www-form-urlencoded"}
HTML = {"Accept": "text/html"}

DOC = (b'<!DOCTYPE html><html><head><title>t</title>'
       b'<meta http-equiv="X-UA-Compatible" content="IE=7">'
       b'<link rel="stylesheet" href="a.css"></head><body>hi</body></html>')

Received = collections.namedtuple("Received", "method path headers body")


def _raw(handler, status, headers, body=b""):
    """Write a response exactly as given: no Date or Server header, no defaults."""
    lines = ["HTTP/1.1 %d X" % status] + ["%s: %s" % pair for pair in headers]
    handler.wfile.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body)


def _doc_headers(length):
    return [("Content-Type", "text/html"), ("Content-Length", str(length)),
            ("ETag", '"v1"'), ("Last-Modified", "Mon, 01 Jan 2001 00:00:00 GMT"),
            ("Cache-Control", "public, max-age=60"), ("X-Custom", "kept")]


def _route_doc(handler):
    _raw(handler, 200, _doc_headers(len(DOC)), DOC)


def _route_fragment(handler):
    _raw(handler, 200, _doc_headers(5), b"OK|A1")


def _route_chunked(handler):
    head = [("Content-Type", "text/html"), ("Transfer-Encoding", "chunked")]
    chunks = [DOC[i:i + 7] for i in range(0, len(DOC), 7)]
    body = b"".join(b"%x\r\n%s\r\n" % (len(c), c) for c in chunks) + b"0\r\n\r\n"
    _raw(handler, 200, head, body)


def _route_closed(handler):
    _raw(handler, 200, [("Content-Type", "text/html"), ("Connection", "close")], DOC)
    handler.close_connection = True


def _route_split(handler):
    handler.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    handler.wfile.write(("HTTP/1.1 200 X\r\nContent-Type: text/html\r\nContent-Length: %d\r\n\r\n"
                         % len(DOC)).encode("latin-1"))
    for i in range(0, len(DOC), 5):
        handler.wfile.write(DOC[i:i + 5])
        time.sleep(0.002)


def _route_attachment(handler):
    _raw(handler, 200, [("Content-Type", "text/html"), ("Content-Length", str(len(DOC))),
                        ("Content-Disposition", 'attachment; filename="a.htm"')], DOC)


def _route_gzip(handler):
    body = gzip.compress(DOC)
    _raw(handler, 200, [("Content-Type", "text/html"), ("Content-Encoding", "gzip"),
                        ("Content-Length", str(len(body)))], body)


def _route_plain(handler):
    _raw(handler, 200, [("Content-Type", "text/plain"), ("Content-Length", str(len(DOC)))], DOC)


def _route_404(handler):
    _raw(handler, 404, [("Content-Type", "text/html"), ("Content-Length", str(len(DOC)))], DOC)


def _route_redirect(handler):
    _raw(handler, 302, [("Location", "http://example.test/next"), ("Content-Length", "0"),
                        ("Content-Type", "text/html")])


def _route_not_modified(handler):
    _raw(handler, 304, [("ETag", '"v1"')])


def _route_cookies(handler):
    _raw(handler, 200, [("Set-Cookie", "a=1; Path=/"), ("Set-Cookie", "b=2; Path=/"),
                        ("Content-Type", "text/plain"), ("Content-Length", "2")], b"ok")


def _route_hop(handler):
    _raw(handler, 407, [("Proxy-Authenticate", 'Basic realm="x"'), ("Connection", "keep-alive"),
                        ("Keep-Alive", "timeout=5"), ("X-Custom", "kept"),
                        ("Content-Length", "0")])


def _route_headers(handler):
    body = "".join("%s: %s\n" % pair for pair in handler.headers.items()).encode("latin-1")
    _raw(handler, 200, [("Content-Type", "text/plain"), ("Content-Length", str(len(body)))], body)


def _route_once(handler):
    """Answer, claim keep-alive, then drop the connection: a stale pooled connection."""
    _raw(handler, 200, [("Content-Type", "text/plain"), ("Content-Length", "2")], b"ok")
    handler.close_connection = True


BODY_FIRST_DOC = b"<body class=a>hello</body>"
FRAMESET_DOC = b'<html><frameset cols="*,*"><frame src="a.htm"><frame src="b.htm"></frameset></html>'
BOM_DOC = (b"\xef\xbb\xbf<!DOCTYPE html><html><head><script>var a;</script></head>"
           b"<body>x</body></html>")
LONG_RUN = b"<!-- " + b"x" * 70000 + b" -->"
LONG_DOC = LONG_RUN + DOC


def _route_body_first(handler):
    _raw(handler, 200, _doc_headers(len(BODY_FIRST_DOC)), BODY_FIRST_DOC)


def _route_frameset(handler):
    _raw(handler, 200, _doc_headers(len(FRAMESET_DOC)), FRAMESET_DOC)


def _route_bom(handler):
    _raw(handler, 200, _doc_headers(len(BOM_DOC)), BOM_DOC)


def _route_long_run(handler):
    """70000 bytes of leading comment at once, the rest only after the test opens the gate."""
    handler.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    _raw(handler, 200, _doc_headers(len(LONG_DOC)), LONG_DOC[:len(LONG_RUN)])
    handler.server.gate.wait(10)
    handler.wfile.write(LONG_DOC[len(LONG_RUN):])


def _route_any(handler):
    body = b"OK|" + handler.path.encode("ascii")
    _raw(handler, 200, [("Content-Type", "text/html; charset=UTF-8"),
                        ("Content-Length", str(len(body)))], body)


ROUTES = {
    "/doc": _route_doc, "/fragment": _route_fragment, "/chunked": _route_chunked,
    "/closed": _route_closed, "/split": _route_split, "/attachment": _route_attachment,
    "/gzip": _route_gzip, "/plain": _route_plain, "/missing": _route_404,
    "/redirect": _route_redirect, "/notmodified": _route_not_modified,
    "/cookies": _route_cookies, "/hop": _route_hop, "/headers": _route_headers,
    "/once": _route_once, "/bodyfirst": _route_body_first, "/frameset": _route_frameset,
    "/bom": _route_bom, "/longrun": _route_long_run,
}


class _QuietServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass  # clients drop connections all the time; that is not worth a traceback


class StubOrigin:
    """A scripted HTTP/1.1 origin. ``received`` lists every request it got."""

    def __init__(self):
        self.received = []
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def handle_any(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                stub.received.append(Received(self.command, self.path, self.headers, body))
                path = urllib.parse.urlsplit(self.path).path
                ROUTES.get(path, _route_any)(self)

            do_GET = do_POST = do_HEAD = handle_any

        self.server = _QuietServer(("127.0.0.1", 0), Handler)
        self.server.gate = threading.Event()  # lets a route wait for the test
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


def _header_list(resp, skip=("date",)):
    return [(k.lower(), v) for k, v in resp.getheaders() if k.lower() not in skip]


class ProxyCase(unittest.TestCase):
    def setUp(self):
        self.fixture = start_fixture_server()
        self.addCleanup(self.fixture.stop)
        self.base = self.fixture.base_url.rstrip("/")

    # -- helpers ---------------------------------------------------------
    def start_proxy(self, instrument_js=None, max_body=262144, **kwargs):
        store = CaptureStore(Redactor(), max_body=max_body)
        proxy = RecordingProxy(store, instrument_js=instrument_js, **kwargs)
        port = proxy.start()
        self.addCleanup(lambda: self.assertEqual(proxy.internal_errors, []))
        self.addCleanup(proxy.stop)
        return proxy, store, port

    def stub(self):
        stub = StubOrigin()
        self.addCleanup(stub.stop)
        return stub

    def connect(self, port):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        self.addCleanup(conn.close)
        return conn

    def via(self, port, method, url, body=None, headers=None, **kwargs):
        conn = self.connect(port)
        conn.request(method, url, body=body, headers=headers or {}, **kwargs)
        resp = conn.getresponse()
        return resp, resp.read()

    def direct(self, url, method="GET", body=None, headers=None):
        parts = urllib.parse.urlsplit(url)
        conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
        self.addCleanup(conn.close)
        conn.request(method, parts.path + ("?" + parts.query if parts.query else ""),
                     body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp, resp.read()

    def wait_for(self, predicate, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            value = predicate()
            if value:
                return value
            time.sleep(0.01)
        self.fail("condition not met within %.0f s" % timeout)

    def wait_entry(self, store, count=1, **filters):
        entries = self.wait_for(lambda: (lambda found: found if len(found) >= count else None)(
            store.list(limit=None, **filters)))
        return entries[count - 1]


class RecordingTests(ProxyCase):
    def test_proxy_records_post_body_and_response(self):
        proxy, store, port = self.start_proxy()
        url = self.base + "/echo"
        resp, data = self.via(port, "POST", url, body="code=A1", headers=FORM)
        self.assertEqual(resp.status, 200)
        entry = self.wait_entry(store, method="POST")
        self.assertEqual(entry.method, "POST")
        self.assertEqual(entry.url, url)
        self.assertEqual(entry.status, 200)
        self.assertIn(b"code=A1", entry.req_body)
        self.assertIn(b"echo_code", entry.resp_body)
        self.assertEqual(entry.resp_body, data)
        self.assertEqual(entry.source, "proxy")
        self.assertFalse(entry.tunnel)
        self.assertFalse(entry.truncated)
        self.assertGreater(entry.started, 0)
        self.assertTrue(any(k.lower() == "content-type" for k, _ in entry.resp_headers))

    def test_seen_and_first_request(self):
        proxy, store, port = self.start_proxy()
        self.assertFalse(proxy.first_request.is_set())
        self.assertEqual(proxy.seen, [])
        self.via(port, "GET", self.base + "/index.htm")
        self.via(port, "GET", self.base + "/fragment")
        self.assertTrue(proxy.first_request.is_set())
        self.assertEqual(proxy.seen, [self.base + "/index.htm", self.base + "/fragment"])

    def test_seen_and_doc_charsets_are_capped_at_1000_most_recent(self):
        stub = self.stub()
        proxy, store, port = self.start_proxy()
        conn = self.connect(port)
        for i in range(1100):
            conn.request("GET", "%s/d/%d" % (stub.base, i), headers=HTML)
            conn.getresponse().read()
        last = "%s/d/1099" % stub.base
        self.wait_for(lambda: last in proxy.doc_charsets)
        self.assertEqual(len(proxy.seen), 1000)
        self.assertEqual(proxy.seen[-1], last)
        self.assertEqual(proxy.seen[0], "%s/d/100" % stub.base)
        self.assertEqual(len(proxy.doc_charsets), 1000)
        self.assertNotIn("%s/d/0" % stub.base, proxy.doc_charsets)
        self.assertEqual(proxy.doc_charsets[last], "utf-8")

    def test_referer_charset_recorded(self):
        proxy, store, port = self.start_proxy()
        url = self.base + "/charset.htm"
        self.via(port, "GET", url, headers=HTML)
        self.wait_for(lambda: url in proxy.doc_charsets)
        self.assertEqual(proxy.doc_charsets[url], "windows-1252")  # from the meta prescan
        headers = dict(FORM, Referer=url)
        self.via(port, "POST", self.base + "/echo", body="name=x", headers=headers)
        entry = self.wait_entry(store, method="POST")
        self.assertEqual(entry.referer_charset, "windows-1252")
        # a request whose referer is not a known document gets none
        self.via(port, "POST", self.base + "/echo", body="name=x",
                 headers=dict(FORM, Referer=self.base + "/unknown.htm"))
        self.assertIsNone(self.wait_entry(store, count=2, method="POST").referer_charset)

    def test_charset_from_content_type_header_wins(self):
        stub = self.stub()
        proxy, store, port = self.start_proxy()
        url = stub.base + "/d/1"
        self.via(port, "GET", url, headers=HTML)
        self.wait_for(lambda: url in proxy.doc_charsets)
        self.assertEqual(proxy.doc_charsets[url], "utf-8")

    def test_unreachable_origin_is_a_502_with_an_error_entry(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
        probe.close()
        proxy, store, port = self.start_proxy()
        resp, _ = self.via(port, "GET", "http://127.0.0.1:%d/x" % closed_port)
        self.assertEqual(resp.status, 502)
        entry = self.wait_entry(store)
        self.assertTrue(entry.error)

    def test_origin_form_request_is_refused_and_not_forwarded(self):
        proxy, store, port = self.start_proxy()
        resp, _ = self.via(port, "GET", "/index.htm", headers={"Host": self.base[7:]})
        self.assertEqual(resp.status, 400)
        resp, _ = self.via(port, "GET", "https://example.test/x")
        self.assertEqual(resp.status, 400)
        self.assertEqual(self.fixture.requests, [])
        # a request the proxy cannot interpret as an exchange is not recorded at all
        self.assertEqual(store.list(limit=None), [])
        self.assertEqual(proxy.seen, [])

    def test_request_to_the_proxy_itself_is_refused_and_recorded(self):
        # Unlike the malformed requests above this is a well-formed exchange the proxy declines
        # after reading it, so it gets an entry (status 508 and the reason).
        proxy, store, port = self.start_proxy()
        url = "http://127.0.0.1:%d/page" % port
        resp, _ = self.via(port, "GET", url)
        self.assertEqual(resp.status, 508)
        entry = self.wait_entry(store)
        self.assertEqual((entry.url, entry.status), (url, 508))
        self.assertIn("proxy itself", entry.error)
        self.assertEqual(self.fixture.requests, [])


class PassThroughTests(ProxyCase):
    def test_proxy_returns_response_unchanged_without_injection(self):
        proxy, store, port = self.start_proxy(instrument_js=None)
        url = self.base + "/index.htm"
        direct, direct_body = self.direct(url, headers=HTML)
        resp, body = self.via(port, "GET", url, headers=HTML)
        self.assertEqual(body, direct_body)
        self.assertEqual(_header_list(resp), _header_list(direct))

    def test_fragment_response_unchanged_with_injection_on(self):
        proxy, store, port = self.start_proxy(instrument_js=b"js")
        url = self.base + "/fragment"
        direct, _ = self.direct(url, headers=HTML)
        resp, body = self.via(port, "GET", url, headers=HTML)
        self.assertEqual(body, b"OK|A1")
        self.assertEqual(_header_list(resp), _header_list(direct))
        # validators and caching headers of a response the proxy did not modify are kept
        stub = self.stub()
        direct, _ = self.direct(stub.base + "/fragment", headers=HTML)
        resp, body = self.via(port, "GET", stub.base + "/fragment", headers=HTML)
        self.assertEqual(body, b"OK|A1")
        self.assertEqual(_header_list(resp), _header_list(direct))
        self.assertEqual(resp.getheader("ETag"), '"v1"')
        self.assertEqual(resp.getheader("Cache-Control"), "public, max-age=60")

    def test_xhr_request_not_injected(self):
        proxy, store, port = self.start_proxy(instrument_js=b"js")
        direct, direct_body = self.direct(self.base + "/echo", method="POST", body="code=A1",
                                          headers=dict(FORM, Accept="*/*"))
        resp, body = self.via(port, "POST", self.base + "/echo", body="code=A1",
                              headers=dict(FORM, Accept="*/*"))
        self.assertIn(b"<html>", body)  # it is a full document, still left alone
        self.assertEqual(body, direct_body)
        self.assertNotIn(b"__iemcp__", body)

    def test_other_responses_pass_through_byte_for_byte(self):
        stub = self.stub()
        proxy, store, port = self.start_proxy(instrument_js=b"js")
        for path in ("/attachment", "/gzip", "/plain", "/missing", "/redirect", "/cookies"):
            with self.subTest(path=path):
                direct, direct_body = self.direct(stub.base + path, headers=HTML)
                resp, body = self.via(port, "GET", stub.base + path, headers=HTML)
                self.assertEqual(resp.status, direct.status)
                self.assertEqual(body, direct_body)
                self.assertEqual(_header_list(resp), _header_list(direct))
        self.assertEqual(resp.msg.get_all("Set-Cookie"), ["a=1; Path=/", "b=2; Path=/"])

    def test_attachment_and_head_not_injected(self):
        stub = self.stub()
        proxy, store, port = self.start_proxy(instrument_js=b"js")
        _, body = self.via(port, "GET", stub.base + "/attachment", headers=HTML)
        self.assertEqual(body, DOC)
        # HEAD: no body, the origin's Content-Length is kept, and the connection stays usable
        url = self.base + "/index.htm"
        direct, _ = self.direct(url, method="HEAD", headers=HTML)
        conn = self.connect(port)
        conn.request("HEAD", url, headers=HTML)
        resp = conn.getresponse()
        self.assertEqual(resp.read(), b"")
        self.assertEqual(resp.getheader("Content-Length"), direct.getheader("Content-Length"))
        conn.request("GET", self.base + "/fragment", headers=HTML)
        self.assertEqual(conn.getresponse().read(), b"OK|A1")

    def test_not_modified_has_no_body_and_keeps_the_connection(self):
        stub = self.stub()
        proxy, store, port = self.start_proxy(instrument_js=b"js")
        conn = self.connect(port)
        conn.request("GET", stub.base + "/notmodified", headers=HTML)
        resp = conn.getresponse()
        self.assertEqual((resp.status, resp.read()), (304, b""))
        conn.request("GET", stub.base + "/fragment", headers=HTML)
        self.assertEqual(conn.getresponse().read(), b"OK|A1")

    def test_hop_by_hop_headers_are_dropped_in_both_directions(self):
        stub = self.stub()
        proxy, store, port = self.start_proxy()
        headers = {"Connection": "keep-alive, X-Hop", "X-Hop": "1", "Keep-Alive": "5",
                   "TE": "trailers", "Upgrade": "websocket", "Proxy-Connection": "keep-alive",
                   "Proxy-Authorization": "Basic Zm9vOmJhcg==", "X-Keep": "yes",
                   "Cookie": "a=b", "Accept": "*/*"}
        _, data = self.via(port, "GET", stub.base + "/headers", headers=headers)
        received = {line.split(":")[0].lower() for line in data.decode().splitlines()}
        for dropped in ("connection", "x-hop", "keep-alive", "te", "upgrade", "proxy-connection",
                        "proxy-authorization"):
            self.assertNotIn(dropped, received)
        for kept in ("x-keep", "cookie", "accept", "host"):
            self.assertIn(kept, received)
        self.assertIn("host: " + stub.base[7:], data.decode().lower())
        # a challenge from an upstream proxy is passed back, the rest of the hop headers are not
        resp, _ = self.via(port, "GET", stub.base + "/hop")
        self.assertEqual(resp.status, 407)
        self.assertEqual(resp.getheader("Proxy-Authenticate"), 'Basic realm="x"')
        self.assertEqual(resp.getheader("X-Custom"), "kept")
        self.assertIsNone(resp.getheader("Keep-Alive"))

    def test_no_cache_strips_conditional_headers(self):
        conditional = {"If-None-Match": '"abc"', "If-Modified-Since": "Mon, 01 Jan 2001 00:00:00 GMT"}
        proxy, store, port = self.start_proxy(no_cache=True)
        self.via(port, "GET", self.base + "/index.htm", headers=conditional)
        headers = self.fixture.requests[-1].headers
        self.assertNotIn("If-None-Match", headers)
        self.assertNotIn("If-Modified-Since", headers)
        # without no_cache they are forwarded
        proxy, store, port = self.start_proxy(no_cache=False)
        self.via(port, "GET", self.base + "/index.htm", headers=conditional)
        self.assertIn("If-None-Match", self.fixture.requests[-1].headers)

    def test_proxy_authorization_is_not_forwarded_without_upstream_proxy(self):
        proxy, store, port = self.start_proxy()
        self.via(port, "GET", self.base + "/index.htm",
                 headers={"Proxy-Authorization": "Basic Zm9vOmJhcg=="})
        self.assertNotIn("Proxy-Authorization", self.fixture.requests[-1].headers)

    def test_request_bodies_large_and_chunked(self):
        proxy, store, port = self.start_proxy(max_body=65536)
        big = b"a=" + b"x" * (2 * 1024 * 1024)
        resp, data = self.via(port, "POST", self.base + "/echo", body=big, headers=FORM)
        self.assertEqual(resp.status, 200)
        self.assertIn(b"echo_a", data)
        self.assertGreater(len(data), len(big))
        entry = self.wait_entry(store, method="POST")
        self.assertEqual(len(entry.req_body), 65536)
        self.assertTrue(entry.truncated)
        # a chunked request body reaches the origin whole, with a Content-Length
        headers = dict(FORM, **{"Transfer-Encoding": "chunked"})
        resp, data = self.via(port, "POST", self.base + "/echo", body=iter([b"code=", b"A1"]),
                              headers=headers, encode_chunked=True)
        self.assertIn(b'<dd id="echo_code">A1</dd>', data)
        received = self.fixture.requests[-1].headers
        self.assertEqual(received.get("Content-Length"), "7")
        self.assertNotIn("Transfer-Encoding", received)


class InjectionTests(ProxyCase):
    def test_document_injected_and_not_cacheable(self):
        js = b"window.__iemcp = 1;"
        proxy, store, port = self.start_proxy(instrument_js=js)
        url = self.base + "/index.htm"
        _, original = self.direct(url)
        resp, body = self.via(port, "GET", url, headers=HTML)
        offset = find_insertion_point(original, True)
        self.assertTrue(original[offset:].startswith(b"<script"))
        self.assertEqual(body, original[:offset] + loader_tag(url) + original[offset:])
        self.assertEqual(body.count(b"/__iemcp__/i.js"), 1)
        self.assertIn(b'src="%s/__iemcp__/i.js"' % self.base.encode(), body)
        self.assertEqual(resp.msg.get_all("Cache-Control"), ["no-store"])
        self.assertIsNone(resp.getheader("ETag"))
        self.assertIsNone(resp.getheader("Last-Modified"))
        length = resp.getheader("Content-Length")
        chunked = (resp.getheader("Transfer-Encoding") or "").lower() == "chunked"
        self.assertTrue(chunked or int(length) == len(body))
        # the entry holds what the origin sent, not the modified copy
        entry = self.wait_entry(store)
        self.assertEqual(entry.resp_body, original)

    def test_modified_response_drops_validators_and_keeps_other_headers(self):
        stub = self.stub()
        proxy, store, port = self.start_proxy(instrument_js=b"js")
        resp, body = self.via(port, "GET", stub.base + "/doc", headers=HTML)
        tag = loader_tag(stub.base + "/doc")
        offset = find_insertion_point(DOC, True)
        self.assertEqual(body, DOC[:offset] + tag + DOC[offset:])
        self.assertIsNone(resp.getheader("ETag"))
        self.assertIsNone(resp.getheader("Last-Modified"))
        self.assertEqual(resp.msg.get_all("Cache-Control"), ["no-store"])
        self.assertEqual(resp.getheader("X-Custom"), "kept")
        self.assertEqual(int(resp.getheader("Content-Length")), len(body))
        entry = self.wait_entry(store)
        self.assertEqual(entry.resp_body, DOC)
        self.assertTrue(any(k.lower() == "etag" for k, _ in entry.resp_headers))

    def test_body_first_document_gets_the_loader_right_before_body(self):
        stub = self.stub()
        proxy, store, port = self.start_proxy(instrument_js=b"js")
        resp, body = self.via(port, "GET", stub.base + "/bodyfirst", headers=HTML)
        self.assertEqual(body, loader_tag(stub.base + "/bodyfirst") + BODY_FIRST_DOC)
        self.assertEqual(int(resp.getheader("Content-Length")), len(body))
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")

    def test_headless_frameset_passes_through_unchanged(self):
        stub = self.stub()
        proxy, store, port = self.start_proxy(instrument_js=b"js")
        direct, _ = self.direct(stub.base + "/frameset", headers=HTML)
        resp, body = self.via(port, "GET", stub.base + "/frameset", headers=HTML)
        self.assertEqual(body, FRAMESET_DOC)
        self.assertEqual(_header_list(resp), _header_list(direct))
        self.assertEqual(resp.getheader("Cache-Control"), "public, max-age=60")  # not no-store
        self.assertEqual(resp.getheader("ETag"), '"v1"')

    def test_bom_and_doctype_stay_untouched_before_the_loader(self):
        stub = self.stub()
        proxy, store, port = self.start_proxy(instrument_js=b"js")
        _, body = self.via(port, "GET", stub.base + "/bom", headers=HTML)
        tag = loader_tag(stub.base + "/bom")
        prefix = b"\xef\xbb\xbf<!DOCTYPE html><html><head>"
        self.assertTrue(body.startswith(prefix + tag + b"<script>"))
        self.assertEqual(body, BOM_DOC[:len(prefix)] + tag + BOM_DOC[len(prefix):])

    def test_run_longer_than_64k_is_passed_through_after_only_a_64k_window(self):
        stub = self.stub()
        proxy, store, port = self.start_proxy(instrument_js=b"js")
        conn = self.connect(port)
        conn.request("GET", stub.base + "/longrun", headers=HTML)
        resp = conn.getresponse()  # the head waits for the 64 KB decision, not for the origin
        first = resp.read(65536)  # and the 64 KB are delivered while the origin is still waiting
        self.assertFalse(stub.server.gate.is_set())
        self.assertEqual(first, LONG_DOC[:65536])
        stub.server.gate.set()
        body = first + resp.read()
        self.assertEqual(body, LONG_DOC)
        direct, _ = self.direct(stub.base + "/longrun", headers=HTML)
        self.assertEqual(_header_list(resp), _header_list(direct))
        self.assertEqual(resp.getheader("Cache-Control"), "public, max-age=60")
        self.assertNotIn(b"__iemcp__", body)

    def test_injection_off_without_instrument_js(self):
        stub = self.stub()
        proxy, store, port = self.start_proxy(instrument_js=None)
        _, body = self.via(port, "GET", stub.base + "/doc", headers=HTML)
        self.assertEqual(body, DOC)

    def test_chunked_close_delimited_and_split_upstream_bodies(self):
        stub = self.stub()
        proxy, store, port = self.start_proxy(instrument_js=b"js")
        offset = find_insertion_point(DOC, True)
        for path in ("/chunked", "/closed", "/split"):
            with self.subTest(path=path):
                conn = self.connect(port)
                conn.request("GET", stub.base + path, headers=HTML)
                resp = conn.getresponse()
                body = resp.read()
                self.assertEqual(body, DOC[:offset] + loader_tag(stub.base + path) + DOC[offset:])
                self.assertEqual(resp.getheader("Cache-Control"), "no-store")
                # the client connection is still usable afterwards
                conn.request("GET", stub.base + "/fragment", headers=HTML)
                self.assertEqual(conn.getresponse().read(), b"OK|A1")
        # without injection a chunked body is delivered byte for byte as well
        proxy, store, port = self.start_proxy(instrument_js=None)
        _, body = self.via(port, "GET", stub.base + "/chunked", headers=HTML)
        self.assertEqual(body, DOC)

    def test_identity_encoding_requested_only_for_documents_that_may_be_modified(self):
        stub = self.stub()
        proxy, store, port = self.start_proxy(instrument_js=b"js")
        gz = {"Accept-Encoding": "gzip, deflate"}
        self.via(port, "GET", stub.base + "/headers", headers=dict(gz, Accept="text/html"))
        self.assertEqual(stub.received[-1].headers.get("Accept-Encoding"), "identity")
        self.via(port, "GET", stub.base + "/headers", headers=dict(gz, Accept="*/*"))
        self.assertEqual(stub.received[-1].headers.get("Accept-Encoding"), "gzip, deflate")
        proxy, store, port = self.start_proxy(instrument_js=None)
        self.via(port, "GET", stub.base + "/headers", headers=dict(gz, Accept="text/html"))
        self.assertEqual(stub.received[-1].headers.get("Accept-Encoding"), "gzip, deflate")

    def test_loader_path_served_by_proxy(self):
        js = b"window.__iemcp = {};"
        proxy, store, port = self.start_proxy(instrument_js=js)
        resp, body = self.via(port, "GET", self.base + "/__iemcp__/i.js")
        self.assertEqual(resp.status, 200)
        self.assertEqual(body, js)
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")
        # on any host, without touching the network
        resp, body = self.via(port, "GET", "http://no-such-host.invalid/__iemcp__/i.js?x=1")
        self.assertEqual((resp.status, body), (200, js))
        self.assertEqual(self.fixture.requests, [])
        self.assertEqual(store.list(limit=None), [])
        self.assertEqual(proxy.seen, [])
        self.assertFalse(proxy.first_request.is_set())

    def test_loader_path_without_instrument_js_is_not_forwarded(self):
        proxy, store, port = self.start_proxy(instrument_js=None)
        resp, _ = self.via(port, "GET", self.base + "/__iemcp__/i.js")
        self.assertEqual(resp.status, 404)
        self.assertEqual(self.fixture.requests, [])
        self.assertEqual(store.list(limit=None), [])

    def test_progressive_head_injected_after_meta(self):
        proxy, store, port = self.start_proxy(instrument_js=b"js")
        url = self.base + "/progressive?ms=800"
        _, original = self.direct(self.base + "/progressive?ms=0")
        conn = self.connect(port)
        start = time.monotonic()
        conn.request("GET", url, headers=HTML)
        resp = conn.getresponse()
        got = b""
        while b"<link" not in got:
            more = resp.read1(65536)
            if not more:
                break
            got += more
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 0.5)
        self.assertIn(b"<link", got)
        xua, tag, link = (got.index(b"X-UA-Compatible"), got.index(b"/__iemcp__/i.js"),
                          got.index(b"<link"))
        self.assertLess(xua, tag)
        self.assertLess(tag, link)
        got += resp.read()
        offset = find_insertion_point(original, True)
        self.assertEqual(got, original[:offset] + loader_tag(url) + original[offset:])


class StreamingTests(ProxyCase):
    def test_streams_large_response(self):
        proxy, store, port = self.start_proxy(max_body=65536)
        conn = self.connect(port)
        start = time.monotonic()
        conn.request("GET", self.base + "/big?ms=1500")
        resp = conn.getresponse()
        first = resp.read(1)
        self.assertLess(time.monotonic() - start, 1.0)
        rest = resp.read()
        self.assertEqual(len(first) + len(rest), 1024 * 1024)
        entry = self.wait_entry(store)
        self.assertEqual(len(entry.resp_body), 65536)
        self.assertTrue(entry.truncated)
        self.assertEqual(entry.resp_body, (BIG_PATTERN * 8)[:65536])

    def test_client_abort_mid_body_is_recorded_and_closes_upstream(self):
        proxy, store, port = self.start_proxy()
        conn = self.connect(port)
        conn.request("GET", self.base + "/big?ms=3000")
        resp = conn.getresponse()
        resp.read(1)
        conn.close()
        entry = self.wait_entry(store)
        self.assertTrue(entry.error)

    def test_client_connection_reuses_one_upstream_connection(self):
        proxy, store, port = self.start_proxy()
        conn = self.connect(port)
        for path in ("/index.htm", "/fragment"):
            conn.request("GET", self.base + path)
            conn.getresponse().read()
        first = [r.conn_id for r in self.fixture.requests]
        self.assertEqual(len(first), 2)
        self.assertEqual(first[0], first[1])
        # another client connection gets an upstream connection of its own
        other = self.connect(port)
        other.request("GET", self.base + "/fragment")
        other.getresponse().read()
        self.assertNotEqual(self.fixture.requests[-1].conn_id, first[0])

    def test_stale_kept_alive_upstream_connection_is_retried_once(self):
        stub = self.stub()
        proxy, store, port = self.start_proxy()
        conn = self.connect(port)
        for _ in range(2):
            conn.request("GET", stub.base + "/once")
            resp = conn.getresponse()
            self.assertEqual((resp.status, resp.read()), (200, b"ok"))
        self.assertEqual(len(stub.received), 2)

    def test_stop_closes_active_connections_promptly(self):
        proxy, store, port = self.start_proxy()
        conn = self.connect(port)
        conn.request("GET", self.base + "/slow?ms=30000")
        self.wait_for(lambda: self.fixture.requests)
        start = time.monotonic()
        proxy.stop()
        self.assertLess(time.monotonic() - start, 5.0)
        with self.assertRaises((http.client.HTTPException, OSError)):
            conn.getresponse()
        proxy.stop()  # twice is safe


class ChainingTests(ProxyCase):
    def test_connect_tunnel_recorded_as_tunnel(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)
        echo_port = listener.getsockname()[1]

        def echo():
            conn, _ = listener.accept()
            with conn:
                while True:
                    data = conn.recv(4096)
                    if not data:
                        return
                    conn.sendall(data)

        thread = threading.Thread(target=echo, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        proxy, store, port = self.start_proxy()
        target = "127.0.0.1:%d" % echo_port
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.addCleanup(sock.close)
        sock.sendall(("CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n" % (target, target)).encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += sock.recv(4096)
        self.assertIn(b" 200 ", head.split(b"\r\n")[0])
        sock.sendall(b"ping-0123")
        echoed = b""
        while len(echoed) < 9:
            echoed += sock.recv(4096)
        self.assertEqual(echoed, b"ping-0123")
        sock.close()
        entry = self.wait_entry(store)
        self.assertTrue(entry.tunnel)
        self.assertEqual(entry.method, "CONNECT")
        self.assertEqual(entry.url, target)
        self.assertEqual(entry.status, 200)
        self.assertIn(target, proxy.seen)
        self.assertTrue(proxy.first_request.is_set())

    def test_upstream_proxy_chaining_passes_proxy_auth(self):
        first, first_store, first_port = self.start_proxy()
        second, second_store, second_port = self.start_proxy(
            upstream_proxy=("127.0.0.1", first_port))
        url = self.base + "/echo"
        resp, data = self.via(second_port, "POST", url, body="code=A1",
                              headers=dict(FORM, **{"Proxy-Authorization": "Basic Zm9vOmJhcg=="}))
        self.assertEqual(resp.status, 200)
        self.assertIn(b"echo_code", data)
        for store in (first_store, second_store):
            entry = self.wait_entry(store, method="POST")
            self.assertEqual(entry.url, url)
            self.assertEqual(entry.status, 200)
        names = [k.lower() for k, _ in self.wait_entry(first_store, method="POST").req_headers]
        self.assertIn("proxy-authorization", names)
        # the origin never sees it
        self.assertNotIn("Proxy-Authorization", self.fixture.requests[-1].headers)

    def test_chain_keeps_one_connection_to_the_upstream_proxy_per_client_connection(self):
        first, first_store, first_port = self.start_proxy()
        second, second_store, second_port = self.start_proxy(
            upstream_proxy=("127.0.0.1", first_port))
        stub = self.stub()
        conn = self.connect(second_port)
        for url in (self.base + "/index.htm", stub.base + "/fragment", self.base + "/fragment"):
            conn.request("GET", url)
            conn.getresponse().read()
        # one client connection to the second proxy -> one connection to the first, whatever
        # the target host (connection-based proxy authentication needs that)
        self.assertEqual(len(set(r.conn_id for r in self.fixture.requests)), 1)

    def test_connect_407_challenge_continues_on_the_same_upstream_connection(self):
        # A connection-based scheme (NTLM) authenticates the TCP connection: the second CONNECT
        # must reach the upstream proxy on the socket that carried the first.
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)
        requests = []

        def read_head(conn):
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return None
                data += chunk
            return data

        def fake_upstream_proxy():
            conn, _ = listener.accept()
            with conn:
                requests.append(read_head(conn))
                conn.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                             b"Proxy-Authenticate: NTLM\r\nContent-Length: 0\r\n\r\n")
                requests.append(read_head(conn))
                conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                data = conn.recv(4096)
                conn.sendall(data.upper())

        thread = threading.Thread(target=fake_upstream_proxy, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        proxy, store, port = self.start_proxy(
            upstream_proxy=("127.0.0.1", listener.getsockname()[1]))
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.addCleanup(sock.close)

        def connect(extra=""):
            sock.sendall(("CONNECT app.example.test:443 HTTP/1.1\r\n"
                          "Host: app.example.test:443\r\n%s\r\n" % extra).encode())
            head = b""
            while b"\r\n\r\n" not in head:
                head += sock.recv(4096)
            return head

        first = connect()
        self.assertIn(b" 407 ", first.split(b"\r\n")[0])
        self.assertIn(b"Proxy-Authenticate: NTLM", first)
        second = connect("Proxy-Authorization: NTLM TlRMTVNTUAAB\r\n")
        self.assertIn(b" 200 ", second.split(b"\r\n")[0])
        sock.sendall(b"abc")
        self.assertEqual(sock.recv(4096), b"ABC")
        self.assertEqual(len(requests), 2)
        self.assertNotIn(b"Proxy-Authorization", requests[0])
        self.assertIn(b"Proxy-Authorization: NTLM TlRMTVNTUAAB", requests[1])
        entries = self.wait_for(lambda: (lambda found: found if len(found) == 2 else None)(
            store.list(limit=None)))
        self.assertEqual([e.status for e in entries], [407, 200])

    def _fake_upstream_proxy(self, answer):
        """An upstream proxy that answers the first CONNECT with ``answer``, then waits for the
        proxy under test to close the connection. ``seen['closed']`` is True if it did."""
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)
        seen = {"closed": None}

        def run():
            conn, _ = listener.accept()
            with conn:
                data = b""
                while b"\r\n\r\n" not in data:
                    data += conn.recv(4096)
                conn.sendall(answer)
                conn.settimeout(3)
                try:
                    seen["closed"] = conn.recv(4096) == b""
                except OSError:
                    seen["closed"] = False

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        return listener.getsockname()[1], seen, thread

    def test_failed_connect_closes_the_upstream_socket(self):
        for answer in (b"garbage\r\n\r\n",  # a malformed answer: the proxy gives up
                       b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                       b"Proxy-Authenticate: Basic\r\nConnection: close\r\n\r\n"):  # not kept
            with self.subTest(answer=answer[:12]), warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                upstream_port, seen, thread = self._fake_upstream_proxy(answer)
                proxy, store, port = self.start_proxy(upstream_proxy=("127.0.0.1", upstream_port))
                sock = socket.create_connection(("127.0.0.1", port), timeout=5)
                self.addCleanup(sock.close)
                sock.sendall(b"CONNECT app.example.test:443 HTTP/1.1\r\n"
                             b"Host: app.example.test:443\r\n\r\n")
                self.assertTrue(sock.recv(4096))  # a 502 or the relayed 407
                thread.join(5)
                self.assertTrue(self.wait_entry(store).tunnel)
                self.wait_for(lambda: not proxy._handlers)  # the handler has finished
                gc.collect()
                # Closed by the proxy itself, not left for the garbage collector (which would
                # warn about an unclosed socket) or for a timeout on the upstream side.
                self.assertIs(seen["closed"], True)
                self.assertEqual([str(w.message) for w in caught
                                  if issubclass(w.category, ResourceWarning)], [])

    def test_connect_through_an_upstream_proxy(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)
        echo_port = listener.getsockname()[1]

        def echo():
            conn, _ = listener.accept()
            with conn:
                data = conn.recv(4096)
                conn.sendall(data.upper())

        thread = threading.Thread(target=echo, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        first, first_store, first_port = self.start_proxy()
        second, second_store, second_port = self.start_proxy(
            upstream_proxy=("127.0.0.1", first_port))
        target = "127.0.0.1:%d" % echo_port
        sock = socket.create_connection(("127.0.0.1", second_port), timeout=5)
        self.addCleanup(sock.close)
        sock.sendall(("CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n" % (target, target)).encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += sock.recv(4096)
        self.assertIn(b" 200 ", head.split(b"\r\n")[0])
        sock.sendall(b"abc")
        self.assertEqual(sock.recv(4096), b"ABC")
        for store in (first_store, second_store):
            self.assertTrue(self.wait_entry(store).tunnel)


if __name__ == "__main__":
    unittest.main()
