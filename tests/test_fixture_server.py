"""Unit tests for the fixture site server (no browser needed)."""

import http.client
import socket
import time
import unittest
import urllib.parse

from ie_mcp_fixtures.server import start_fixture_server


class FixtureServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = start_fixture_server()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def request(self, method, path, body=None, headers=None, base=None):
        parts = urllib.parse.urlsplit(base or self.server.base_url)
        conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            return resp, resp.read()
        finally:
            conn.close()

    def test_serves_static_page(self):
        resp, body = self.request("GET", "/index.htm")
        self.assertEqual(resp.status, 200)
        self.assertIn(b"Fixture index", body)

    def test_echo_lists_posted_fields(self):
        resp, body = self.request(
            "POST", "/echo", body="code=A1&pwd=x",
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(resp.status, 200)
        text = body.decode("utf-8")
        self.assertIn('<dd id="echo_code">A1</dd>', text)
        self.assertIn('<dd id="echo_pwd">x</dd>', text)
        self.assertIn('<dd id="echo_raw">code=A1&amp;pwd=x</dd>', text)

    def test_lookup_xml_filters_by_prefix(self):
        resp, body = self.request("GET", "/api/lookup.xml?q=B")
        self.assertEqual(resp.status, 200)
        self.assertTrue(resp.getheader("Content-Type").startswith("text/xml"))
        self.assertIn(b'code="B2"', body)
        self.assertNotIn(b'code="A1"', body)

    def test_lookup_xml_empty_query_returns_all(self):
        _, body = self.request("GET", "/api/lookup.xml?q=")
        for code in (b'code="A1"', b'code="B2"', b'code="C3"'):
            self.assertIn(code, body)

    def test_cookie_endpoint_sets_session_cookie(self):
        resp, _ = self.request("GET", "/cookie")
        self.assertEqual(resp.getheader("Set-Cookie"), "sid=fixture-session; Path=/")

    def test_charset_page_is_windows_1252(self):
        _, body = self.request("GET", "/charset.htm")
        self.assertIn(b"M\xfcller", body)

    def test_responses_are_not_cacheable(self):
        for path in ("/index.htm", "/api/lookup.xml?q=", "/cookie", "/slow?ms=1"):
            with self.subTest(path=path):
                resp, _ = self.request("GET", path)
                self.assertEqual(resp.getheader("Cache-Control"), "no-store")
                self.assertIsNone(resp.getheader("ETag"))
                self.assertIsNone(resp.getheader("Last-Modified"))

    def test_error_responses_are_not_cacheable(self):
        for method, path in (("GET", "/missing.htm"), ("PUT", "/index.htm"),
                             ("POST", "/index.htm")):
            with self.subTest(method=method, path=path):
                resp, _ = self.request(method, path, body=b"" if method != "GET" else None)
                self.assertGreaterEqual(resp.status, 400)
                self.assertEqual(resp.getheader("Cache-Control"), "no-store")

    def test_path_traversal_is_refused(self):
        for path in ("/../server.py", "/%2e%2e/server.py", "/..%5cserver.py"):
            with self.subTest(path=path):
                resp, body = self.request("GET", path)
                self.assertEqual(resp.status, 404)
                self.assertNotIn(b"start_fixture_server", body)

    def test_echo_decodes_windows_1252_post(self):
        # A windows-1252 page posts its form in windows-1252, not UTF-8.
        _, body = self.request(
            "POST", "/echo", body="name=M%FCller+%96+caf%E9",
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertIn('<dd id="echo_name">Müller – café</dd>', body.decode("utf-8"))

    def test_kind_options_are_labelled_by_their_value(self):
        # Later tasks select the option by its visible label ("B").
        _, body = self.request("GET", "/form.htm")
        for value in "ABC":
            self.assertIn('<option value="%s">%s</option>' % (value, value), body.decode("ascii"))

    def test_fragment_endpoint_is_bare_text(self):
        resp, body = self.request("GET", "/fragment")
        self.assertTrue(resp.getheader("Content-Type").startswith("text/html"))
        self.assertEqual(body, b"OK|A1")

    def test_big_endpoint_streams(self):
        parts = urllib.parse.urlsplit(self.server.base_url)
        conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
        try:
            start = time.monotonic()
            conn.request("GET", "/big?ms=500")
            resp = conn.getresponse()
            first = resp.read(8192)
            first_at = time.monotonic() - start
            rest = resp.read()
            done_at = time.monotonic() - start
        finally:
            conn.close()
        self.assertEqual(len(first), 8192)
        self.assertLess(first_at, 0.4)
        self.assertEqual(len(first) + len(rest), 1048576)
        # spread over the ms: a proxy that buffers the whole body must be visible
        self.assertGreaterEqual(done_at, 0.4)

    def test_alt_origin_uses_other_host(self):
        alt = self.server.alt_base_url
        self.assertTrue(alt.startswith("http://localhost:"))
        _, body = self.request("GET", "/xorigin.htm")
        self.assertIn(alt + "inner.htm", body.decode("utf-8"))
        alt_port = urllib.parse.urlsplit(alt).port
        resp, _ = self.request("GET", "/inner.htm", base="http://127.0.0.1:%d/" % alt_port)
        self.assertEqual(resp.status, 200)

    def test_new_pages_served(self):
        _, nomode = self.request("GET", "/nomode.htm")
        self.assertTrue(nomode.startswith(b"<!DOCTYPE html>"))
        self.assertIn(b"no mode meta", nomode)
        self.assertNotIn(b"X-UA-Compatible", nomode)
        _, headless = self.request("GET", "/headless.htm")
        self.assertNotIn(b"<head", headless.lower())
        self.assertIn(b'<html><meta http-equiv="X-UA-Compatible" content="IE=7">', headless)
        self.assertIn(b"headless ok", headless)
        _, late = self.request("GET", "/latecharset.htm")
        self.assertIn(b"M\xfcller", late)
        self.assertIn(b"M\xfcller \x96 caf\xe9", late)
        meta_at = late.index(b"charset=windows-1252")
        self.assertLess(late.index(b'<link rel="stylesheet" href="data:text/css,">'), meta_at)
        self.assertLess(late.index(b"<script"), meta_at)

    def test_dialog_and_xhr_pages_have_new_controls(self):
        _, dialog = self.request("GET", "/modal_dialog.htm")
        _, xhr = self.request("GET", "/xhr.htm")
        _, index = self.request("GET", "/index.htm")
        for marker in (b'id="search"', b'id="search_count"', b'id="dialog_alert"',
                       b'id="dialog_alert_result"'):
            self.assertIn(marker, dialog)
        for marker in (b'id="load_fragment"', b'id="fragment_result"'):
            self.assertIn(marker, xhr)
        for page in (dialog, index):
            for marker in (b'id="typed"', b'id="keypress_count"', b'id="typed_chars"'):
                self.assertIn(marker, page)

    def test_requests_are_recorded(self):
        self.server.clear_requests()
        self.request("GET", "/fragment", headers={"X-Test": "1"})
        last = self.server.requests[-1]
        self.assertEqual(last.method, "GET")
        self.assertEqual(last.path, "/fragment")
        self.assertEqual(last.headers["X-Test"], "1")
        # header names compare case-insensitively, like HTTP
        self.assertEqual(last.headers["x-test"], "1")
        self.assertIn("x-TEST", last.headers)
        self.server.clear_requests()
        self.assertEqual(self.server.requests, [])

        parts = urllib.parse.urlsplit(self.server.base_url)
        conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
        try:
            for _ in range(2):
                conn.request("GET", "/fragment")
                conn.getresponse().read()
        finally:
            conn.close()
        self.request("GET", "/fragment")
        ids = [r.conn_id for r in self.server.requests]
        self.assertEqual(len(ids), 3)
        self.assertEqual(ids[0], ids[1])
        self.assertNotEqual(ids[1], ids[2])

    def test_progressive_endpoint_flushes_head_early(self):
        from ie_mcp_fixtures.server import PROGRESSIVE_PARTS
        parts = urllib.parse.urlsplit(self.server.base_url)
        sock = socket.create_connection((parts.hostname, parts.port), timeout=10)
        meta = b'<meta http-equiv="X-UA-Compatible" content="IE=7">'
        data = b""

        def read_until(marker):
            nonlocal data
            while marker not in data:
                chunk = sock.recv(65536)
                self.assertTrue(chunk, "connection closed before %r arrived" % marker)
                data += chunk
            return time.monotonic() - start

        try:
            start = time.monotonic()
            sock.sendall(b"GET /progressive?ms=800 HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            read_until(b"</title>")
            # the head arrives in separate writes: the meta is not there yet
            self.assertNotIn(meta, data)
            meta_at = read_until(meta)
            self.assertNotIn(b"<link", data)
            link_at = read_until(b'<link rel="stylesheet" href="data:text/css,">')
            self.assertNotIn(b"progressive ok", data)
            tail_at = read_until(b"progressive ok</body></html>")
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
        finally:
            sock.close()
        self.assertLess(meta_at, 0.5)
        self.assertLess(link_at, 0.5)
        self.assertGreaterEqual(tail_at, 0.7)
        head, _, body = data.partition(b"\r\n\r\n")
        self.assertIn(b"text/html", head)
        self.assertEqual(body, b"".join(PROGRESSIVE_PARTS))
        self.assertIn(b"Content-Length: %d" % len(body), head)

    def test_alt_origin_placeholder_replaced(self):
        _, body = self.request("GET", "/xorigin.htm")
        text = body.decode("utf-8")
        self.assertIn(self.server.alt_base_url + "inner.htm", text)
        self.assertNotIn("{{ALT_BASE}}", text)
        base_port = urllib.parse.urlsplit(self.server.base_url).port
        alt_port = urllib.parse.urlsplit(self.server.alt_base_url).port
        self.assertNotEqual(base_port, alt_port)


def ipv6_loopback_available():
    """Independent of the server: can this machine bind ::1 at all?"""
    if not socket.has_ipv6:
        return False
    probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    try:
        probe.bind(("::1", 0))
        return True
    except OSError:
        return False
    finally:
        probe.close()


class FixtureFilesTests(unittest.TestCase):
    def test_site_files_have_lf_line_endings(self):
        # The pages are served byte for byte; .gitattributes keeps git from
        # converting them, so a CR here would be committed and served as is.
        from ie_mcp_fixtures.server import SITE_DIR
        offenders = [p.name for p in SITE_DIR.iterdir() if b"\r" in p.read_bytes()]
        self.assertEqual(offenders, [])


class HeaderDictTests(unittest.TestCase):
    def make(self):
        from ie_mcp_fixtures.server import HeaderDict
        return HeaderDict([("X-Test", "1"), ("Accept", "text/html")])

    def test_lookup_is_case_insensitive(self):
        h = self.make()
        self.assertEqual(h["x-test"], "1")
        self.assertEqual(h.get("ACCEPT"), "text/html")
        self.assertIsNone(h.get("missing"))
        self.assertIn("x-TEST", h)
        self.assertEqual(sorted(h), ["Accept", "X-Test"])

    def test_is_read_only(self):
        h = self.make()
        for mutate in (lambda: h.__setitem__("X-New", "2"), lambda: h.__delitem__("X-Test"),
                       lambda: h.update({"x-test": "2"}), lambda: h.pop("X-Test"),
                       lambda: h.setdefault("X-New", "2"), lambda: h.clear(),
                       lambda: h.popitem(), lambda: h.__ior__({"x-test": "2"})):
            with self.assertRaises(TypeError):
                mutate()
        self.assertEqual(h["x-test"], "1")

    def test_copy_and_pickle_keep_case_insensitivity(self):
        import copy
        import pickle
        from ie_mcp_fixtures.server import HeaderDict
        h = self.make()
        for clone in (h.copy(), copy.copy(h), copy.deepcopy(h), pickle.loads(pickle.dumps(h))):
            self.assertIsInstance(clone, HeaderDict)
            self.assertEqual(clone["x-test"], "1")
            self.assertEqual(dict(clone), dict(h))


class FixturePortTests(unittest.TestCase):
    def test_stop_closes_open_keepalive_connections(self):
        server = start_fixture_server()
        parts = urllib.parse.urlsplit(server.base_url)
        conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=5)
        try:
            conn.request("GET", "/fragment")
            self.assertEqual(conn.getresponse().read(), b"OK|A1")
            server.stop()
            with self.assertRaises((OSError, http.client.HTTPException)):
                conn.request("GET", "/fragment")
                conn.getresponse().read()
        finally:
            conn.close()
            server.stop()
        self.assertEqual(len(server.requests), 1)

    def test_stop_refuses_connections_accepted_during_stop(self):
        from unittest import mock
        from ie_mcp_fixtures import server as srv_mod

        real_setup = srv_mod._FixtureHandler.setup

        def slow_setup(handler):
            time.sleep(0.3)  # the handler reaches setup() only after stop() began
            real_setup(handler)

        with mock.patch.object(srv_mod._FixtureHandler, "setup", slow_setup):
            server = start_fixture_server()
            parts = urllib.parse.urlsplit(server.base_url)
            sock = socket.create_connection((parts.hostname, parts.port), timeout=5)
            try:
                server.stop()
                time.sleep(0.4)
                try:
                    sock.sendall(b"GET /fragment HTTP/1.1\r\nHost: x\r\n\r\n")
                    reply = sock.recv(65536)
                except OSError:
                    reply = b""
            finally:
                sock.close()
        self.assertNotIn(b"200 OK", reply)

    def test_stop_is_quick(self):
        server = start_fixture_server()
        start = time.monotonic()
        server.stop()
        self.assertLess(time.monotonic() - start, 0.9)

    def test_busy_port_is_not_shared(self):
        # Windows SO_REUSEADDR would let a second server bind the same port.
        with start_fixture_server() as first:
            busy = urllib.parse.urlsplit(first.base_url).port
            with self.assertRaises(OSError):
                start_fixture_server(port=busy).stop()

    def test_same_port_for_both_origins_is_rejected(self):
        with self.assertRaises(ValueError):
            start_fixture_server(port=50999, alt_port=50999).stop()

    def test_localhost_host_is_rejected(self):
        # base and alt would share the host name "localhost": one origin for IE.
        with self.assertRaises(ValueError):
            start_fixture_server(host="localhost").stop()

    def test_alt_port_answers_on_ipv6_loopback(self):
        if not ipv6_loopback_available():
            self.skipTest("no IPv6 loopback on this machine")
        with start_fixture_server() as server:
            self.assertEqual(len(server._servers), 3, "no ::1 listener for the alt port")
            port = urllib.parse.urlsplit(server.alt_base_url).port
            conn = http.client.HTTPConnection("::1", port, timeout=10)
            try:
                conn.request("GET", "/inner.htm")
                self.assertEqual(conn.getresponse().status, 200)
            finally:
                conn.close()

    def test_alt_port_retries_when_ipv6_port_is_taken(self):
        import errno
        from unittest import mock
        from ie_mcp_fixtures import server as srv_mod

        real = srv_mod._ExclusiveHTTPServer6
        calls = []

        def flaky(addr, handler):
            calls.append(addr[1])
            if len(calls) == 1:
                raise OSError(errno.EADDRINUSE, "taken")
            return real(addr, handler)

        with mock.patch.object(srv_mod.socket, "has_ipv6", True), \
                mock.patch.object(srv_mod, "_ExclusiveHTTPServer6", side_effect=flaky):
            server = start_fixture_server()
            with server:
                port = urllib.parse.urlsplit(server.alt_base_url).port
                if ipv6_loopback_available():
                    self.assertEqual(len(server._servers), 3)
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[1], port)
                # the first IPv4 socket was closed: its port can be bound again
                probe = socket.socket()
                try:
                    probe.bind(("127.0.0.1", calls[0]))
                finally:
                    probe.close()


if __name__ == "__main__":
    unittest.main()
