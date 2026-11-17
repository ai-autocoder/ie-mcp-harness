"""Unit tests for the fixture site server (no browser needed)."""

import http.client
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

    def test_alt_origin_placeholder_replaced(self):
        _, body = self.request("GET", "/xorigin.htm")
        text = body.decode("utf-8")
        self.assertIn(self.server.alt_base_url + "inner.htm", text)
        self.assertNotIn("{{ALT_BASE}}", text)
        base_port = urllib.parse.urlsplit(self.server.base_url).port
        alt_port = urllib.parse.urlsplit(self.server.alt_base_url).port
        self.assertNotEqual(base_port, alt_port)


class FixturePortTests(unittest.TestCase):
    def test_busy_port_is_not_shared(self):
        # Windows SO_REUSEADDR would let a second server bind the same port.
        with start_fixture_server() as first:
            busy = urllib.parse.urlsplit(first.base_url).port
            with self.assertRaises(OSError):
                start_fixture_server(port=busy).stop()

    def test_same_port_for_both_origins_is_rejected(self):
        with self.assertRaises(ValueError):
            start_fixture_server(port=50999, alt_port=50999).stop()


if __name__ == "__main__":
    unittest.main()
