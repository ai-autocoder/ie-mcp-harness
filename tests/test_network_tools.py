"""Unit tests for Task 13: capture wiring, the network tools and ie_status reporting.

No browser: the session's driver and proxy are fakes (``SESSION.ensure`` is patched so that a
test can never start a real browser). A real ``RecordingProxy`` is started only on
``127.0.0.1`` and is always stopped again.
"""
import dataclasses
import gzip
import json
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

import ie_mcp.tools  # noqa: F401  (importing the package registers every tool)
from ie_mcp import config, protocol, session
from ie_mcp.capture import verify
from ie_mcp.capture.redact import Redactor
from ie_mcp.capture.store import CaptureStore, NetworkEntry
from ie_mcp.session import SESSION, IeSession
from ie_mcp.tools import console as consoletools
from ie_mcp.tools import diagnostics, navigation, network

NO_BROWSER = AssertionError("the test started a browser through SESSION.ensure()")


def settings_for(**env):
    """Settings from ``env`` (a home that does not exist: never scan the real Selenium cache)."""
    return config.load_settings(
        {k: str(v) for k, v in env.items()},
        home=Path(tempfile.gettempdir()) / "ie-mcp-tests-no-such-home-13")


def first_text(result):
    return result["content"][0]["text"]


def all_texts(result):
    return [item["text"] for item in result["content"]]


class FakeProxy:
    """What ``SESSION.proxy`` needs: ``seen``, ``internal_errors``, ``instrument_js``, ``stop``."""

    def __init__(self, seen=(), instrument_js=b"js"):
        self.seen = list(seen)
        self.internal_errors = []
        self.instrument_js = instrument_js
        self.port = 4242
        self.stopped = 0

    def stop(self):
        self.stopped += 1


class FakeDriver:
    title = "Page"
    current_url = "http://127.0.0.1/"

    def __init__(self):
        self.got = []

    def get(self, url):
        self.got.append(url)


class SessionStateTestCase(unittest.TestCase):
    """Patches the shared SESSION and the settings, restores both afterwards."""

    def setUp(self):
        self.patch(SESSION, "driver", None)
        self.patch(SESSION, "ensure", mock.Mock(side_effect=NO_BROWSER))
        self.patch(SESSION, "proxy", None)
        self.patch(SESSION, "store", None)
        self.patch(SESSION, "capture_active", None)
        self.patch(SESSION, "capture_error", None)
        self.patch(SESSION, "initial_url", None)
        self.patch(SESSION, "proxy_override", [])
        self.patch(session, "VERIFY_WAIT", 0)
        self.patch(navigation.time, "sleep", lambda seconds: None)
        for name in ("POST_HOOKS", "AFTER_CALL"):
            self.addCleanup(setattr, protocol, name, getattr(protocol, name))
            setattr(protocol, name, [])

    def patch(self, target, name, value):
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def use_settings(self, **changes):
        self.patch(config, "SETTINGS", dataclasses.replace(config.SETTINGS, **changes))

    def use_driver(self):
        driver = FakeDriver()
        self.patch(SESSION, "driver", driver)
        self.patch(SESSION, "ensure", lambda *a, **k: driver)
        return driver

    def use_proxy(self, seen=(), **kwargs):
        proxy = FakeProxy(seen, **kwargs)
        self.patch(SESSION, "proxy", proxy)
        return proxy

    def use_store(self):
        store = CaptureStore(Redactor())
        self.patch(SESSION, "store", store)
        return store


# --- settings -------------------------------------------------------------------------------
class SettingsTests(unittest.TestCase):
    def test_capture_settings_defaults(self):
        settings = settings_for()
        self.assertTrue(settings.capture)  # spike D12 = no: the proxy is on by default
        self.assertIsNone(settings.capture_upstream_proxy)
        self.assertEqual(settings.capture_bypass, ())
        self.assertFalse(settings.capture_no_cache)
        self.assertFalse(settings.keep_artifacts)

    def test_capture_settings_from_the_environment(self):
        settings = settings_for(
            IE_CAPTURE="0", IE_CAPTURE_UPSTREAM_PROXY="http://gateway.example.org:3128",
            IE_CAPTURE_BYPASS="*.corp.example, intranet ,", IE_CAPTURE_NO_CACHE="1",
            IE_KEEP_ARTIFACTS="1")
        self.assertFalse(settings.capture)
        self.assertEqual(settings.capture_upstream_proxy, ("gateway.example.org", 3128))
        self.assertEqual(settings.capture_bypass, ("*.corp.example", "intranet"))
        self.assertTrue(settings.capture_no_cache)
        self.assertTrue(settings.keep_artifacts)

    def test_a_malformed_upstream_proxy_does_not_stop_the_server_from_loading(self):
        # The server must still start and list its tools; the session start reports it.
        for bad in ("gateway.example.org", "gateway.example.org:notaport", ":3128",
                    "gateway.example.org:0"):
            with self.subTest(bad=bad):
                settings = settings_for(IE_CAPTURE_UPSTREAM_PROXY=bad)
                self.assertIsNone(settings.capture_upstream_proxy)
                self.assertIn("IE_CAPTURE_UPSTREAM_PROXY", settings.capture_upstream_proxy_error)
                self.assertNotIn("\n", settings.capture_upstream_proxy_error)
                with self.assertRaisesRegex(RuntimeError, "IE_CAPTURE_UPSTREAM_PROXY"):
                    IeSession.check_capture_settings(settings)

    def test_a_valid_or_unused_upstream_proxy_raises_nothing(self):
        IeSession.check_capture_settings(settings_for(IE_CAPTURE_UPSTREAM_PROXY="gw.example.org:3128"))
        IeSession.check_capture_settings(settings_for())
        # With capture off the value is never used, so it is not an error either.
        IeSession.check_capture_settings(
            settings_for(IE_CAPTURE="0", IE_CAPTURE_UPSTREAM_PROXY="nonsense"))


# --- url matching ----------------------------------------------------------------------------
class UrlMatchTests(unittest.TestCase):
    def test_verification_normalises_urls(self):
        self.assertTrue(verify.url_seen("HTTP://Host:80/a#x", ["http://host/a"]))
        self.assertTrue(verify.url_seen("http://host/a", ["HTTP://HOST:80/a"]))
        self.assertTrue(verify.url_seen("http://host:8080/a?b=1", ["http://host:8080/a?b=1"]))
        self.assertFalse(verify.url_seen("http://host/a", ["http://host/b"]))
        self.assertFalse(verify.url_seen("http://host:8080/a", ["http://host:8081/a"]))

    def test_an_empty_path_is_a_slash(self):
        self.assertTrue(verify.url_seen("http://host", ["http://host/"]))

    def test_a_query_that_ie_rewrote_still_counts_on_the_path(self):
        self.assertTrue(verify.url_seen("http://host/a?q=x y", ["http://host/a?q=x%20y"]))

    def test_https_needs_a_connect_to_the_host_and_port(self):
        self.assertTrue(verify.url_seen("https://Host/a", ["host:443"]))
        self.assertTrue(verify.url_seen("https://host:8443/a", ["host:8443"]))
        self.assertFalse(verify.url_seen("https://host/a", ["host:8443"]))
        self.assertFalse(verify.url_seen("https://host/a", ["http://host/a"]))

    def test_only_http_and_https_are_targets(self):
        for url in ("about:blank", "file:///c:/x.htm", "javascript:void(0)", "", None):
            self.assertIsNone(verify.http_target(url), url)
        self.assertEqual(verify.http_target("HTTP://Host:80/a#x"), ("http", "host", 80))

    def test_host_patterns(self):
        self.assertTrue(verify.host_matches("app.corp.example", ["*.corp.example"]))
        self.assertTrue(verify.host_matches("APP.corp.example", ["*.CORP.example"]))
        self.assertFalse(verify.host_matches("example.org", ["*.corp.example"]))
        self.assertFalse(verify.host_matches("intranet", []))

    def test_proxy_override_entries(self):
        entries = ["*.corp.example", "<local>", "10.*", "http://legacy.example:8080"]
        for host in ("app.corp.example", "intranet", "10.1.2.3", "legacy.example"):
            self.assertTrue(verify.override_matches(host, entries), host)
        self.assertFalse(verify.override_matches("example.org", entries))
        # <local> means dotless hosts only, and only when it is listed.
        self.assertFalse(verify.override_matches("intranet", ["*.corp.example"]))
        self.assertFalse(verify.override_matches("example.org", ["<local>"]))

    def test_proxy_override_is_read_read_only_from_hkcu_internet_settings(self):
        fake = mock.Mock()
        fake.HKEY_CURRENT_USER = "HKCU"
        fake.KEY_READ = 0x20019
        fake.OpenKey.return_value.__enter__ = lambda self: "key"
        fake.OpenKey.return_value.__exit__ = lambda self, *a: False
        fake.QueryValueEx.return_value = ("*.Corp.example;<local>; ;10.*", 1)
        with mock.patch.dict("sys.modules", {"winreg": fake}):
            entries = verify.read_proxy_override()
        self.assertEqual(entries, ["*.corp.example", "<local>", "10.*"])
        args = fake.OpenKey.call_args.args
        self.assertEqual(args[0], "HKCU")
        self.assertEqual(args[1], r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        self.assertEqual(args[3], fake.KEY_READ)
        # Nothing was written: only the two read calls were made on the module.
        called = {name for name, *_ in fake.method_calls}
        self.assertLessEqual(called, {"OpenKey", "QueryValueEx"})

    def test_a_missing_value_or_registry_means_no_entries(self):
        fake = mock.Mock()
        fake.OpenKey.side_effect = FileNotFoundError()
        with mock.patch.dict("sys.modules", {"winreg": fake}):
            self.assertEqual(verify.read_proxy_override(), [])
        with mock.patch.dict("sys.modules", {"winreg": None}):  # not Windows: import fails
            self.assertEqual(verify.read_proxy_override(), [])


# --- ie_network / ie_request ----------------------------------------------------------------
def entry(**kwargs):
    base = dict(started=1.0, duration_ms=12, method="POST", url="http://127.0.0.1:1/echo",
                status=200, resp_body=b"x" * 345, source="proxy")
    base.update(kwargs)
    return NetworkEntry(**base)


class NetworkToolTests(SessionStateTestCase):
    def test_network_tool_line_format(self):
        store = self.use_store()
        store.add(entry())
        text = first_text(network.t_network({}))
        self.assertEqual(text.splitlines()[0], "1 proxy POST 200 12 345 http://127.0.0.1:1/echo")

    def test_network_tool_filters_and_pages(self):
        store = self.use_store()
        store.add(entry(url="http://h/a", method="GET", status=200))
        store.add(entry(url="http://h/b", method="POST", status=500))
        store.add(entry(url="http://h/c", method="GET", status=200))
        lines = first_text(network.t_network({"method": "get", "limit": 1})).splitlines()
        self.assertEqual([line.split()[-1] for line in lines[:1]], ["http://h/a"])
        text = first_text(network.t_network({"status": 500}))
        self.assertIn("http://h/b", text)
        self.assertNotIn("http://h/a", text)
        text = first_text(network.t_network({"url_contains": "/c", "since_id": 1}))
        self.assertIn("http://h/c", text)
        text = first_text(network.t_network({"since_id": 2}))
        self.assertIn("http://h/c", text)
        self.assertNotIn("http://h/b", text)

    def test_paging_note_only_when_more_entries_follow(self):
        store = self.use_store()
        for name in "abc":
            store.add(entry(url="http://h/" + name))
        self.assertNotIn("since_id=", first_text(network.t_network({"limit": 3})))
        text = first_text(network.t_network({"limit": 2}))
        self.assertIn("since_id=2", text)
        # The filter counts: nothing matching follows, so no note.
        self.assertNotIn("since_id=", first_text(network.t_network({"limit": 1, "url_contains": "/c"})))

    def test_page_entries_show_their_source_and_no_response(self):
        store = self.use_store()
        store.add(NetworkEntry(method="POST", url="https://h/x", source="page", status=200))
        line = first_text(network.t_network({})).splitlines()[0]
        self.assertEqual(line.split()[:4], ["1", "page", "POST", "200"])

    def test_network_rejects_bad_arguments(self):
        self.use_store()
        for args in ({"limit": 0}, {"since_id": -1}, {"status": "ok"}, {"limit": True}):
            with self.subTest(args=args):
                self.assertTrue(network.t_network(args).get("isError"))

    def test_tunnel_entries_add_the_https_note(self):
        store = self.use_store()
        store.add(NetworkEntry(method="CONNECT", url="app.example:443", tunnel=True, status=200))
        self.assertIn("HTTPS: tunnel only (host, timing)", first_text(network.t_network({})))
        store.clear()
        store.add(entry())
        self.assertNotIn("HTTPS: tunnel only", first_text(network.t_network({})))

    def test_empty_store_explains_the_browser_cache(self):
        self.use_store()
        self.use_driver()
        text = first_text(network.t_network({}))
        self.assertIn("responses served from the browser cache are not seen "
                      "(IE_CAPTURE_NO_CACHE=1 turns revalidations into full fetches; "
                      "fresh cache hits stay invisible)", text)

    def test_filtered_out_entries_do_not_claim_an_empty_store(self):
        store = self.use_store()
        store.add(entry())
        text = first_text(network.t_network({"url_contains": "nothing"}))
        self.assertIn("no matching entries", text)
        self.assertNotIn("browser cache", text)

    def test_no_session_yet_is_an_empty_list_not_an_error(self):
        result = network.t_network({})
        self.assertFalse(result.get("isError"))

    def test_off_and_blind_capture_are_said_in_the_listing(self):
        self.use_store()
        self.use_settings(capture=False)
        self.assertIn("IE_CAPTURE=0", first_text(network.t_network({})))
        self.use_settings(capture=True)
        self.patch(SESSION, "capture_active", False)
        self.assertIn("did not use the proxy", first_text(network.t_network({})))

    def test_network_clear(self):
        store = self.use_store()
        store.add(entry())
        text = first_text(network.t_network_clear({}))
        self.assertIn("1", text)
        self.assertEqual(store.list(), [])

    def test_request_body_decoded_with_page_charset(self):
        store = self.use_store()
        store.add(entry(
            req_headers=[("Content-Type", "application/x-www-form-urlencoded")],
            req_body=b"name=M%FCller", referer_charset="windows-1252"))
        text = first_text(network.t_request({"id": 1}))
        self.assertIn("Müller", text)

    def test_utf8_body_decoded(self):
        store = self.use_store()
        store.add(entry(
            req_headers=[("Content-Type", "application/x-www-form-urlencoded; charset=utf-8")],
            req_body=b"name=M%C3%BCller"))
        self.assertIn("Müller", first_text(network.t_request({"id": 1})))

    def test_content_type_charset_wins_over_the_referer_charset(self):
        store = self.use_store()
        store.add(entry(
            req_headers=[("Content-Type", "application/x-www-form-urlencoded; charset=utf-8")],
            req_body=b"name=M%C3%BCller", referer_charset="windows-1252"))
        self.assertIn("Müller", first_text(network.t_request({"id": 1})))

    def test_charset_falls_back_to_cp1252(self):
        store = self.use_store()
        store.add(entry(resp_headers=[("Content-Type", "text/html")], resp_body=b"M\xfcller"))
        self.assertIn("Müller", first_text(network.t_request({"id": 1})))

    def test_an_unknown_charset_name_falls_back(self):
        store = self.use_store()
        store.add(entry(resp_headers=[("Content-Type", "text/html; charset=no-such-set")],
                        resp_body=b"M\xfcller"))
        self.assertIn("Müller", first_text(network.t_request({"id": 1})))

    def test_binary_bodies_are_summarised(self):
        store = self.use_store()
        store.add(entry(resp_headers=[("Content-Type", "image/png")], resp_body=b"\x89PNG\r\n\x00\x01"))
        store.add(entry(resp_headers=[("Content-Type", "text/plain")], resp_body=b"ab\x00cd"))
        self.assertIn("<binary 8 bytes>", first_text(network.t_request({"id": 1})))
        self.assertIn("<binary 5 bytes>", first_text(network.t_request({"id": 2})))

    def test_gzip_and_deflate_bodies_are_decoded_from_a_copy(self):
        store = self.use_store()
        text = "M\u00fcller caf\u00e9 " * 20
        raw = text.encode("utf-8")
        store.add(entry(resp_headers=[("Content-Type", "text/html; charset=utf-8"),
                                      ("Content-Encoding", "gzip")], resp_body=gzip.compress(raw)))
        store.add(entry(resp_headers=[("Content-Type", "text/html; charset=utf-8"),
                                      ("Content-Encoding", "deflate")], resp_body=zlib.compress(raw)))
        raw_deflate = zlib.compressobj(wbits=-15)
        store.add(entry(resp_headers=[("Content-Type", "text/html; charset=utf-8"),
                                      ("Content-Encoding", "deflate")],
                        resp_body=raw_deflate.compress(raw) + raw_deflate.flush()))
        for entry_id in (1, 2, 3):
            with self.subTest(entry_id=entry_id):
                self.assertIn("Müller café", first_text(network.t_request({"id": entry_id})))
        # The stored bytes are untouched (the decode was of a copy).
        self.assertEqual(store.get(1).resp_body, gzip.compress(raw))

    def test_a_decoded_body_cut_at_the_cap_says_so(self):
        store = self.use_store()
        big = gzip.compress(b"a" * (network.MAX_DECODED + 100))
        store.add(entry(resp_headers=[("Content-Type", "text/plain"),
                                      ("Content-Encoding", "gzip")], resp_body=big))
        store.add(entry(resp_headers=[("Content-Type", "text/plain"),
                                      ("Content-Encoding", "gzip")],
                        resp_body=gzip.compress(b"a" * 100)))
        text = first_text(network.t_request({"id": 1, "body_limit": 10}))
        self.assertIn("the decoded body was cut at %d bytes" % network.MAX_DECODED, text)
        self.assertNotIn("was cut at", first_text(network.t_request({"id": 2})))

    def test_an_undecodable_compressed_body_is_said_plainly(self):
        store = self.use_store()
        store.add(entry(resp_headers=[("Content-Encoding", "gzip")], resp_body=b"not gzip at all"))
        text = first_text(network.t_request({"id": 1}))
        self.assertIn("gzip", text)
        self.assertIn("could not be decoded", text)

    def test_body_limit_truncates_the_decoded_text(self):
        store = self.use_store()
        store.add(entry(resp_headers=[("Content-Type", "text/plain")], resp_body=b"a" * 100))
        text = first_text(network.t_request({"id": 1, "body_limit": 10}))
        self.assertIn("a" * 10, text)
        self.assertNotIn("a" * 11, text)
        self.assertIn("90 more characters", text)

    def test_request_shows_headers_status_and_both_bodies(self):
        store = self.use_store()
        store.add(entry(req_headers=[("Content-Type", "text/plain")], req_body=b"ping",
                        resp_headers=[("Content-Type", "text/plain")], resp_body=b"pong"))
        text = first_text(network.t_request({"id": 1}))
        for part in ("POST http://127.0.0.1:1/echo", "200", "Content-Type: text/plain",
                     "ping", "pong"):
            self.assertIn(part, text)

    def test_tunnel_entry_has_no_bodies(self):
        store = self.use_store()
        store.add(NetworkEntry(method="CONNECT", url="app.example:443", tunnel=True, status=200))
        text = first_text(network.t_request({"id": 1}))
        self.assertIn("tunnel", text.lower())

    def test_request_errors(self):
        self.use_store()
        self.assertTrue(network.t_request({"id": 99}).get("isError"))
        self.assertTrue(network.t_request({}).get("isError"))
        self.assertTrue(network.t_request({"id": "x"}).get("isError"))
        self.assertTrue(network.t_request({"id": 1, "body_limit": -1}).get("isError"))

    def test_network_tools_never_need_the_driver(self):
        for name in ("ie_network", "ie_request", "ie_network_clear"):
            spec = protocol.REGISTRY[name]
            self.assertFalse(spec.needs_driver({}), name)


# --- the session: capture on navigation -------------------------------------------------------
class VerificationTests(SessionStateTestCase):
    def open(self, url, **args):
        return navigation.t_open({"url": url, "wait": 0, **args})

    def goto(self, url):
        return navigation.t_goto({"url": url, "wait": 0})

    def test_capture_unverified_for_about_blank(self):
        self.use_driver()
        self.use_proxy()
        self.use_store()
        self.open("about:blank")
        self.assertIsNone(SESSION.capture_active)
        self.assertEqual(diagnostics.status_info()["capture"], "unverified")

    def test_a_new_session_leaves_capture_unverified(self):
        # Spike D11: the initial URL is not proxied, so creating a session cannot verify.
        driver = FakeDriver()
        self.patch(SESSION, "ensure", lambda *a, **k: setattr(SESSION, "driver", driver) or driver)
        self.use_proxy()
        result = self.open("http://127.0.0.1:9/index.htm")
        self.assertIsNone(SESSION.capture_active)
        self.assertEqual(len(result["content"]), 1)  # no NOTICE
        self.assertEqual(driver.got, [])  # the initial URL went in through ensure()

    def test_file_and_about_navigations_leave_it_unverified(self):
        self.use_driver()
        self.use_proxy()
        for url in ("about:blank", "file:///c:/x.htm"):
            self.open(url)
            self.goto(url)
            self.assertIsNone(SESSION.capture_active, url)

    def test_capture_off_when_url_not_seen(self):
        driver = self.use_driver()
        self.use_proxy(seen=[])
        result = self.open("http://127.0.0.1:9/a.htm")
        self.assertIs(SESSION.capture_active, False)
        self.assertIn("NOTICE: network capture is off: the browser did not use the proxy",
                      all_texts(result))
        self.assertEqual(driver.got, ["http://127.0.0.1:9/a.htm"])

    def test_capture_verified_when_the_url_was_seen(self):
        self.use_driver()
        self.use_proxy(seen=["http://127.0.0.1:9/a.htm"])
        result = self.goto("http://127.0.0.1:9/a.htm#frag")
        self.assertIs(SESSION.capture_active, True)
        self.assertEqual(len(result["content"]), 1)

    def test_verification_normalises_urls_end_to_end(self):
        self.use_driver()
        self.use_proxy(seen=["http://host/a"])
        self.goto("HTTP://Host:80/a#x")
        self.assertIs(SESSION.capture_active, True)

    def test_https_is_verified_by_connect(self):
        self.use_driver()
        self.use_proxy(seen=["secure.example:443"])
        self.goto("https://secure.example/login")
        self.assertIs(SESSION.capture_active, True)

    def test_verification_waits_for_a_late_request(self):
        self.use_driver()
        proxy = self.use_proxy(seen=[])
        self.patch(session, "VERIFY_WAIT", 2)
        timer = __import__("threading").Timer(0.3, proxy.seen.append, ["http://h.example/a"])
        timer.start()
        self.addCleanup(timer.cancel)
        self.goto("http://h.example/a")
        self.assertIs(SESSION.capture_active, True)

    def test_a_bypassed_host_is_not_verified(self):
        self.use_driver()
        self.use_proxy(seen=[])
        self.use_settings(capture_bypass=("*.corp.example",))
        result = self.goto("http://app.corp.example/")
        self.assertIsNone(SESSION.capture_active)
        self.assertEqual(len(result["content"]), 1)

    def test_the_initial_url_cannot_prove_the_proxy_is_blind(self):
        # A cache hit for the page the browser started on never reaches the proxy.
        self.use_driver()
        self.use_proxy(seen=[])
        self.patch(SESSION, "initial_url", "http://127.0.0.1:9/index.htm")
        self.goto("http://127.0.0.1:9/index.htm#again")
        self.assertIsNone(SESSION.capture_active)

    def test_a_blind_session_recovers_when_the_proxy_later_sees_the_url(self):
        self.use_driver()
        proxy = self.use_proxy(seen=[])
        self.goto("http://h.example/a")
        self.assertIs(SESSION.capture_active, False)
        proxy.seen.append("http://h.example/b")
        self.goto("http://h.example/b")
        self.assertIs(SESSION.capture_active, True)

    def test_no_proxy_means_nothing_to_verify(self):
        self.use_driver()
        result = self.goto("http://h.example/a")
        self.assertIsNone(SESSION.capture_active)
        self.assertEqual(len(result["content"]), 1)

    def test_zone_notice_for_proxy_override_host(self):
        self.use_driver()
        self.patch(SESSION, "proxy_override", ["*.corp.example", "<local>"])
        self.use_proxy(seen=["http://app.corp.example/", "http://intranet/", "http://example.org/"])
        notice = ("NOTICE: %s normally bypasses your proxy. Through the capture proxy, IE may "
                  "put it in another security zone (different document mode, Protected Mode, "
                  "logon). If the page behaves differently, set IE_CAPTURE_BYPASS=%s.")
        for host in ("app.corp.example", "intranet"):
            result = self.goto("http://%s/" % host)
            self.assertIn(notice % (host, host), all_texts(result), host)
        result = self.goto("http://example.org/")
        self.assertEqual(len(result["content"]), 1)

    def test_no_zone_notice_for_a_bypassed_or_unproxied_host(self):
        self.use_driver()
        self.patch(SESSION, "proxy_override", ["<local>"])
        self.use_proxy(seen=["http://intranet/"])
        self.use_settings(capture_bypass=("intranet",))
        self.assertEqual(len(self.goto("http://intranet/")["content"]), 1)
        # Capture found blind: the page was not proxied, so no zone change to warn about.
        self.use_settings(capture_bypass=())
        self.patch(SESSION, "capture_active", False)
        self.patch(SESSION, "proxy", FakeProxy(seen=[]))
        texts = all_texts(self.goto("http://intranet/"))
        self.assertFalse([t for t in texts if "bypasses your proxy" in t])

    def test_open_on_an_existing_session_navigates_and_verifies(self):
        driver = self.use_driver()
        self.use_proxy(seen=["http://h.example/a"])
        self.open("http://h.example/a")
        self.assertIs(SESSION.capture_active, True)
        self.assertEqual(driver.got, ["http://h.example/a"])

    def test_a_wait_that_is_not_a_number_is_sanitised_after_the_navigation(self):
        # The budget already used number_arg; the pause must too, or wait: "3" raised a
        # TypeError after a navigation that worked.
        driver = self.use_driver()
        driver.back = driver.forward = lambda: None
        self.use_proxy(seen=["http://h.example/a", "http://h.example/b"])
        fake_time = mock.Mock()
        self.patch(navigation, "time", fake_time)
        for result in (navigation.t_open({"url": "http://h.example/a", "wait": "1"}),
                       navigation.t_goto({"url": "http://h.example/b", "wait": "soon"}),
                       navigation.t_back({"wait": True}), navigation.t_forward({"wait": -1})):
            self.assertFalse(result.get("isError"), result)
        self.assertEqual([c.args[0] for c in fake_time.sleep.call_args_list],
                         [1.0, navigation.GOTO_WAIT, 2.0, 2.0])

    def test_open_and_goto_budgets_include_the_verification_wait(self):
        self.patch(session, "VERIFY_WAIT", 5)
        for name, wait in (("ie_open", navigation.OPEN_WAIT), ("ie_goto", navigation.GOTO_WAIT)):
            spec = protocol.REGISTRY[name]
            expected = config.SETTINGS.attach_retries * 75 + wait + 15 + 5
            self.assertEqual(spec.budget({}), expected, name)


# --- the session: proxy lifecycle -----------------------------------------------------------
class CaptureLifecycleTests(SessionStateTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.session_dir = Path(self.tmp.name) / "sess"
        paths = dataclasses.replace(
            config.SETTINGS.session_paths, directory=self.session_dir,
            root=Path(self.tmp.name), lock_file=self.session_dir / "session.lock",
            log_file=self.session_dir / "ie-mcp.log", owner_file=self.session_dir / "owner.json")
        self.use_settings(session_paths=paths, capture=True, instrument=True, console_stub=False,
                          capture_jsonl=False, keep_artifacts=False, capture_upstream_proxy=None,
                          capture_bypass=(), capture_no_cache=False, use_lock=False)
        self.patch(session, "REDACTOR", Redactor())
        self.patch(session, "log", lambda *args: None)  # keep the test output clean
        self.patch(verify, "read_proxy_override", lambda: ["<local>"])
        self.sessions = []

    def new_session(self):
        s = IeSession()
        self.sessions.append(s)
        self.addCleanup(s._stop_capture)
        return s

    def test_start_capture_binds_loopback_and_sets_the_loader_script(self):
        s = self.new_session()
        s._start_capture(config.SETTINGS, "http://127.0.0.1:9/")
        self.assertIsNotNone(s.proxy)
        self.assertEqual(s.proxy.host, "127.0.0.1")
        self.assertGreater(s.proxy.port, 0)
        self.assertIsNone(s.capture_active)
        self.assertEqual(s.proxy_override, ["<local>"])
        self.assertEqual(s.initial_url, "http://127.0.0.1:9/")
        js = s.proxy.instrument_js.decode("utf-8")
        self.assertIn("__iemcp_wrap_xhr=false", js)
        self.assertIn("__iemcp_stub=false", js)
        self.assertIn("window.__iemcp", js)  # instrument.js itself

    def test_the_loader_script_carries_the_console_stub_flag(self):
        self.use_settings(console_stub=True)
        s = self.new_session()
        s._start_capture(config.SETTINGS, None)
        self.assertIn("__iemcp_stub=true", s.proxy.instrument_js.decode("utf-8"))

    def test_no_instrumentation_means_no_loader_script(self):
        self.use_settings(instrument=False)
        s = self.new_session()
        s._start_capture(config.SETTINGS, None)
        self.assertIsNotNone(s.proxy)
        self.assertIsNone(s.proxy.instrument_js)

    def test_capture_off_starts_no_proxy_but_keeps_the_store(self):
        self.use_settings(capture=False)
        s = self.new_session()
        s._start_capture(config.SETTINGS, None)
        self.assertIsNone(s.proxy)
        self.assertIsNotNone(s.store)  # page XHR entries still need somewhere to go
        self.assertIsNone(s.capture_active)

    def test_settings_reach_the_proxy(self):
        self.use_settings(capture_upstream_proxy=("gateway.example.org", 3128),
                          capture_no_cache=True)
        s = self.new_session()
        s._start_capture(config.SETTINGS, None)
        self.assertEqual(s.proxy.upstream_proxy, ("gateway.example.org", 3128))
        self.assertTrue(s.proxy.no_cache)
        self.assertIs(s.proxy.store, s.store)

    def test_a_proxy_that_cannot_start_means_capture_off_not_a_crash(self):
        s = self.new_session()
        with mock.patch.object(session.RecordingProxy, "start", side_effect=OSError("in use")):
            s._start_capture(config.SETTINGS, None)
        self.assertIsNone(s.proxy)
        self.assertIs(s.capture_active, False)
        self.assertIn("in use", s.capture_error)

    def test_stop_capture_closes_the_listening_socket(self):
        s = self.new_session()
        s._start_capture(config.SETTINGS, None)
        port = s.proxy.port
        s._stop_capture()
        self.assertIsNone(s.proxy)
        import socket
        with self.assertRaises(OSError), socket.create_connection(("127.0.0.1", port), timeout=1):
            pass

    def test_starting_twice_never_leaves_two_proxies(self):
        s = self.new_session()
        s._start_capture(config.SETTINGS, None)
        first = s.proxy
        s._start_capture(config.SETTINGS, None)
        self.assertIsNot(s.proxy, first)
        self.assertIsNone(first._server)  # stopped

    def test_store_is_shared_and_survives_a_new_browser(self):
        s = self.new_session()
        s._start_capture(config.SETTINGS, None)
        store = s.store
        store.add(entry())
        s._stop_capture()
        s._start_capture(config.SETTINGS, None)
        self.assertIs(s.store, store)
        self.assertEqual(len(store.list()), 1)

    def test_a_new_body_limit_makes_a_new_store(self):
        s = self.new_session()
        s._start_capture(config.SETTINGS, None)
        old = s.store
        self.use_settings(capture_max_body=10)
        s._start_capture(config.SETTINGS, None)
        self.assertIsNot(s.store, old)
        self.assertEqual(s.store.max_body, 10)

    def build(self, s, **env_changes):
        """``_build`` with a fake driver; returns the options IEDriver would have got."""
        from selenium import webdriver
        exe = self.session_dir.parent / "IEDriverServer.exe"
        exe.write_bytes(b"")
        self.use_settings(driver_path=str(exe), **env_changes)
        fake = mock.Mock()
        with mock.patch.object(webdriver, "Ie", return_value=fake) as ie, \
                mock.patch.object(session, "apply_driver_timeout"):
            built = s._build("http://127.0.0.1:9/", before_profiles=set())
        return built, ie.call_args.kwargs["options"]

    def test_build_points_the_browser_at_the_proxy_with_the_bypass_list(self):
        s = self.new_session()
        _, options = self.build(s, capture_bypass=("*.corp.example",))
        caps = options.to_capabilities()
        proxy = caps["proxy"]
        self.assertEqual(proxy["httpProxy"], "127.0.0.1:%d" % s.proxy.port)
        self.assertEqual(proxy["noProxy"], ["<-loopback>", "*.corp.example"])
        self.assertTrue(options.use_per_process_proxy)

    def test_build_without_capture_sets_no_proxy_capability(self):
        s = self.new_session()
        _, options = self.build(s, capture=False)
        self.assertNotIn("proxy", options.to_capabilities())

    def test_a_failed_build_stops_the_proxy(self):
        from selenium import webdriver
        s = self.new_session()
        exe = self.session_dir.parent / "IEDriverServer.exe"
        exe.write_bytes(b"")
        self.use_settings(driver_path=str(exe))
        with mock.patch.object(webdriver, "Ie", side_effect=RuntimeError("boom")), \
                mock.patch.object(session, "list_ie_browsers", return_value=[]):
            with self.assertRaises(RuntimeError):
                s._build("http://127.0.0.1:9/", before_profiles=set())
        self.assertIsNone(s.proxy)

    def test_quit_browser_force_kill_and_close_stop_the_proxy(self):
        for stop in (lambda s: s.quit_browser(), lambda s: s.force_kill(), lambda s: s.close()):
            with self.subTest(stop=stop):
                s = self.new_session()
                s._start_capture(config.SETTINGS, None)
                proxy = s.proxy  # _stop_capture forgets it first: keep it to see it really stopped
                self.assertIsNotNone(proxy._server)
                port = proxy.port
                stop(s)
                self.assertIsNone(s.proxy)
                self.assertEqual(s.capture_active, None)
                self.assertIsNone(proxy._server)
                import socket
                with self.assertRaises(OSError), socket.create_connection(
                        ("127.0.0.1", port), timeout=1):
                    pass

    def test_ensure_refuses_a_malformed_upstream_proxy_before_building_anything(self):
        self.use_settings(capture_upstream_proxy_error=(
            "IE_CAPTURE_UPSTREAM_PROXY must be host:port (for example gateway.example.org:3128)"))
        s = self.new_session()
        with mock.patch.object(s, "_build", side_effect=AssertionError("built a browser")), \
                self.assertRaisesRegex(RuntimeError, "IE_CAPTURE_UPSTREAM_PROXY must be host:port"):
            s.ensure(initial_url="http://127.0.0.1:9/")
        self.assertIsNone(s.proxy)

    def test_build_refuses_a_malformed_upstream_proxy_and_starts_nothing(self):
        s = self.new_session()
        with self.assertRaisesRegex(RuntimeError, "IE_CAPTURE_UPSTREAM_PROXY"):
            s._start_capture(settings_for(IE_CAPTURE_UPSTREAM_PROXY="nonsense"), None)
        self.assertIsNone(s.proxy)

    def jsonl_session(self, keep):
        self.use_settings(capture_jsonl=True, keep_artifacts=keep)
        s = self.new_session()
        s._start_capture(config.SETTINGS, None)
        s.store.add(entry())
        path = s.store.jsonl_path
        self.assertTrue(path.is_file())
        return s, path

    def test_jsonl_deleted_on_close_unless_kept(self):
        s, path = self.jsonl_session(keep=False)
        s.quit_browser()
        self.assertTrue(path.is_file())  # ie_close is not the end of the process
        s.close()
        self.assertFalse(path.exists())

        s, path = self.jsonl_session(keep=True)
        s.close()
        self.assertTrue(path.is_file())

    def test_the_jsonl_file_lives_in_the_session_directory(self):
        s, path = self.jsonl_session(keep=False)
        self.assertEqual(path.parent, self.session_dir)
        s.close()

    def test_page_xhr_records_land_in_the_session_store(self):
        s = self.new_session()
        s._start_capture(config.SETTINGS, None)
        self.patch(SESSION, "store", s.store)
        raw = "0\x1fxhr\x1f\x1f1.5\x1fPOST http://h.example/echo status=200 resp_len=4 body=q=ping"
        consoletools.ingest(raw, "driver")
        entries = s.store.list()
        self.assertEqual([(e.source, e.method, e.url) for e in entries],
                         [("page", "POST", "http://h.example/echo")])


# --- page flags and ie_status ----------------------------------------------------------------
class PageFlagTests(SessionStateTestCase):
    def test_xhr_is_wrapped_when_capture_is_off_or_blind(self):
        self.assertFalse(consoletools.page_flags()["wrap_xhr"])
        self.use_settings(capture=False)
        self.assertTrue(consoletools.page_flags()["wrap_xhr"])
        self.use_settings(capture=True)
        self.patch(SESSION, "capture_active", False)
        self.assertTrue(consoletools.page_flags()["wrap_xhr"])
        self.patch(SESSION, "capture_active", True)
        self.assertFalse(consoletools.page_flags()["wrap_xhr"])


class StatusTests(SessionStateTestCase):
    def status(self):
        return diagnostics.status_info()

    def test_capture_states(self):
        self.use_settings(capture=False)
        self.assertEqual(self.status()["capture"], "off")
        self.use_settings(capture=True)
        self.use_proxy()
        self.assertEqual(self.status()["capture"], "unverified")
        self.patch(SESSION, "capture_active", True)
        self.assertEqual(self.status()["capture"], "proxy")
        self.patch(SESSION, "capture_active", False)
        self.assertEqual(self.status()["capture"], "page-only")

    def test_capture_note_names_the_gaps_and_the_zone_risk(self):
        self.use_proxy()
        note = self.status()["capture_note"]
        self.assertIn("initial URL", note)
        self.assertIn("fresh browser-cache hits", note)
        self.assertIn("the proxy can change the IE security zone of intranet hosts; use "
                      "IE_CAPTURE_BYPASS or IE_CAPTURE=0 if a page behaves differently", note)
        self.assertNotIn("popup", note)  # spike D11: popups and dialogs are proxied

    def test_capture_note_when_blind_or_off(self):
        self.use_settings(capture=False)
        self.assertIn("IE_CAPTURE=0", self.status()["capture_note"])
        self.use_settings(capture=True)
        self.patch(SESSION, "capture_active", False)
        self.patch(SESSION, "capture_error", "OSError: in use")
        note = self.status()["capture_note"]
        self.assertIn("did not use the proxy", note)
        self.assertIn("OSError: in use", note)

    def test_status_lists_modifications(self):
        self.use_proxy()
        mods = self.status()["modifications"]
        text = "\n".join(mods)
        self.assertIn("Loader <script>", text)
        self.assertIn("Accept-Encoding: identity", text)
        self.assertIn("Cache-Control: no-store", text)
        self.assertIn("Proxy in the request path", text)
        self.assertIn("window.onerror", text)
        self.assertIn("showModalDialog", text)
        self.assertIn("XMLHttpRequest", text)
        self.assertIn("native input without window focus", text)
        for row in mods:  # every row says how to switch it off
            self.assertIn("off:", row)
        self.assertNotIn("Conditional request headers", text)  # IE_CAPTURE_NO_CACHE=0
        self.assertNotIn("no-op console", text)  # IE_CONSOLE_STUB=0

    def test_capture_note_says_what_ie_capture_bypass_changes(self):
        self.use_proxy()
        self.assertNotIn("IE_CAPTURE_BYPASS (", self.status()["capture_note"])
        self.use_settings(capture_bypass=("*.corp.example", "intranet"))
        note = self.status()["capture_note"]
        self.assertIn("IE_CAPTURE_BYPASS (*.corp.example, intranet)", note)
        self.assertIn("no loader", note)
        self.assertIn("native XHR is not recorded", note)

    def test_a_malformed_upstream_proxy_shows_in_status(self):
        self.use_settings(capture_upstream_proxy_error="IE_CAPTURE_UPSTREAM_PROXY must be host:port")
        info = self.status()
        self.assertEqual(info["capture_config_error"], "IE_CAPTURE_UPSTREAM_PROXY must be host:port")
        self.assertIn("IE_CAPTURE_UPSTREAM_PROXY", info["capture_note"])
        self.use_settings(capture=False)
        self.assertNotIn("capture_config_error", self.status())

    def test_proxy_rows_are_omitted_when_the_browser_did_not_use_the_proxy(self):
        self.use_proxy()
        rows = ("Loader <script>", "Accept-Encoding", "Cache-Control: no-store",
                "Proxy in the request path")
        for active in (None, True):
            self.patch(SESSION, "capture_active", active)
            text = "\n".join(self.status()["modifications"])
            for row in rows:
                self.assertIn(row, text, (active, row))
        self.patch(SESSION, "capture_active", False)
        text = "\n".join(self.status()["modifications"])
        for row in rows:
            self.assertNotIn(row, text, row)
        self.assertIn("window.onerror", text)  # the page instrumentation still applies

    def test_per_call_rows_are_always_listed_with_their_trigger(self):
        self.use_settings(capture=False, instrument=False)
        text = "\n".join(self.status()["modifications"])
        self.assertIn("only for ie_click mode=async; default IE_CLICK_MODE=%s"
                      % config.SETTINGS.click_mode, text)
        self.assertIn("only with a hwnd:/tab: target", text)
        self.assertGreaterEqual(text.count("only with a hwnd:/tab: target"), 2)  # input + wrapper

    def test_off_switches_remove_their_rows(self):
        self.use_settings(capture=False, instrument=False)
        text = "\n".join(self.status()["modifications"])
        for gone in ("Loader <script>", "Accept-Encoding", "Cache-Control", "Proxy in the request",
                     "window.onerror", "alert", "showModalDialog", "XMLHttpRequest", "__iemcp_stub"):
            self.assertNotIn(gone, text, gone)

    def test_capture_without_instrumentation_has_no_loader_rows(self):
        # The proxy only modifies documents when it has a script to insert.
        self.use_settings(capture=True, instrument=False)
        self.use_proxy(instrument_js=None)
        text = "\n".join(self.status()["modifications"])
        self.assertNotIn("Loader <script>", text)
        self.assertNotIn("Accept-Encoding", text)
        self.assertNotIn("Cache-Control: no-store", text)
        self.assertIn("Proxy in the request path", text)

    def test_optional_rows_follow_their_switches(self):
        self.use_settings(capture_no_cache=True, console_stub=True, interaction_mode=dataclasses.replace(
            config.SETTINGS.interaction_mode, name="native"))
        self.use_proxy()
        text = "\n".join(self.status()["modifications"])
        self.assertIn("Conditional request headers", text)
        self.assertIn("no-op console", text)
        self.assertNotIn("native input without window focus", text)

    def test_proxy_internal_errors_are_surfaced_and_redacted(self):
        proxy = self.use_proxy()
        self.patch(session, "REDACTOR", Redactor())
        self.assertNotIn("capture_internal_errors", self.status())
        proxy.internal_errors.extend(["KeyError: 'a'", "ValueError: password=hunter22 bad"])
        info = self.status()["capture_internal_errors"]
        self.assertEqual(info["count"], 2)
        self.assertIn("ValueError", info["last"])
        self.assertNotIn("hunter22", info["last"])

    def test_status_tool_reports_the_new_fields_even_without_a_browser(self):
        self.use_proxy()
        result = protocol.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": "ie_status", "arguments": {}}})["result"]
        info = json.loads(first_text(result))
        self.assertFalse(info["alive"])
        for key in ("capture", "capture_note", "modifications"):
            self.assertIn(key, info)


if __name__ == "__main__":
    unittest.main()
