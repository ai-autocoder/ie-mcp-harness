"""Network capture, early injection and the network tools in a real IE-mode window (Task 13).

Spike D11: the session's initial URL (``initial_browser_url``) is not proxied; everything the
browser loads afterwards is. Every page measured here is therefore reached with ``ie_goto``
after the session exists, and the session starts on a page that no test measures.
"""
import json
import re
import time
import unittest

from tests.integration.base import IntegrationTestCase

START_PAGE = "grid.htm"
LINE = re.compile(r"^(\d+) (\w+) (\S+) (\S+) (\S+) (\S+) (.*)$")


class NetworkTests(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(self.close_open_windows)
        # The session (and its store) outlives a test: start each one with an empty list.
        self.text("ie_network_clear")

    # -- helpers -------------------------------------------------------------
    def restart_without_capture(self):
        """A new browser with IE_CAPTURE=0, closed again when the test ends: the session
        outlives a test, and the next one must not inherit a browser that has no proxy."""
        self.restart_session(IE_CAPTURE="0")
        # Cleanups run last-in first-out: this closes the browser before the environment is
        # restored (registered in setUp), so the next test starts its own with the defaults.
        self.addCleanup(self.text, "ie_close")

    def click_until(self, element_id, effect, attempts=4, patience=4):
        """Click a button until ``effect()`` is truthy (its value is returned).

        Native input without window focus loses about 1 click in 6 (spike D17; CLAUDE.md:
        check a click's effect), so a test that needs the click's consequence clicks again when
        nothing happened within ``patience`` seconds.
        """
        for _ in range(attempts):
            self.text("ie_click", by="id", value=element_id, wait=1)
            deadline = time.monotonic() + patience
            while True:
                value = effect()
                if value or time.monotonic() >= deadline:
                    break
                time.sleep(0.25)
            if value:
                return value
        self.fail(f"clicking {element_id} {attempts} times had no effect")

    def post_entry(self, url_contains="/echo"):
        return next(iter(self.entries(url_contains=url_contains, method="POST")), None)

    def start(self, page):
        """A session that starts on START_PAGE (not proxied), then navigates to ``page``."""
        self.open_page(START_PAGE)
        self.text("ie_goto", url=self.base_url + page, wait=1)

    def entries(self, **filters):
        """``ie_network`` as (id, source, method, status, url) tuples."""
        out = []
        for line in self.text("ie_network", **filters).splitlines():
            match = LINE.match(line)
            if match:
                out.append((int(match.group(1)), match.group(2), match.group(3),
                            match.group(4), match.group(7)))
        return out

    def wait_entry(self, url_contains, method=None, source=None):
        filters = {"url_contains": url_contains}
        if method:
            filters["method"] = method

        def find():
            for entry in self.entries(**filters):
                if source is None or entry[1] == source:
                    return entry
            return None
        return self.eventually(find, message=f"a network entry for {url_contains}")

    def js(self, script):
        return json.loads(self.text("ie_js", script=script))

    def console_records(self, **arguments):
        return json.loads(self.text("ie_console", **arguments))["records"]

    # -- tests ---------------------------------------------------------------
    def test_capture_is_unverified_until_the_first_navigation(self):
        self.restart_session()  # a new browser: ie_open on a live session would navigate it
        self.open_page(START_PAGE)
        status = json.loads(self.text("ie_status"))
        self.assertEqual(status["capture"], "unverified")
        self.assertIn("initial URL", status["capture_note"])
        result = self.call("ie_goto", url=self.base_url + "index.htm", wait=1)
        self.assertEqual(len(self.texts(result)), 1, self.texts(result))  # no NOTICE
        status = json.loads(self.text("ie_status"))
        self.assertEqual(status["capture"], "proxy")
        mods = "\n".join(status["modifications"])
        self.assertIn("Loader <script>", mods)
        self.assertIn("Proxy in the request path", mods)
        self.assertEqual(status.get("capture_internal_errors"), None)

    def test_form_post_captured_with_redaction(self):
        self.start("form.htm")
        self.text("ie_fill", by="id", value="code", text="A1")
        self.text("ie_fill", by="id", value="pwd", text="s3cret")
        entry = self.click_until("send", self.post_entry)
        request = self.text("ie_request", id=entry[0])
        self.assertIn("code=A1", request)
        self.assertNotIn("s3cret", request)  # request AND the echoed response
        self.assertEqual(entry[1], "proxy")

    def test_activex_xhr_captured(self):
        self.start("xhr.htm")
        entry = self.click_until("load", self.post_entry)
        self.assertIn("q=ping", self.text("ie_request", id=entry[0]))

    def xhr_results(self):
        fragment = self.click_until(
            "load_fragment", lambda: self.text("ie_get", by="id", value="fragment_result") or None)
        xhr = self.click_until(
            "load", lambda: self.text("ie_get", by="id", value="xhr_result") or None)
        return fragment, xhr

    def test_xhr_fragment_identical_with_capture_on_and_off(self):
        self.start("xhr.htm")
        with_capture = self.xhr_results()
        self.assertEqual(with_capture[0], "OK|A1")
        self.restart_without_capture()
        self.start("xhr.htm")
        self.assertEqual(self.xhr_results(), with_capture)

    def test_native_xhr_recorded_as_page_entry(self):
        self.restart_without_capture()
        self.start("xhr.htm")
        status = json.loads(self.text("ie_status"))
        self.assertEqual(status["capture"], "off")
        self.assertNotIn("Loader <script>", "\n".join(status["modifications"]))

        def page_entry():
            self.call("ie_text")  # the post-call hook drains the page's records
            return next((e for e in self.entries(url_contains="/echo") if e[1] == "page"), None)
        entry = self.click_until("load_native", page_entry)
        self.assertEqual(entry[2], "POST")

    def test_dialog_document_requests_captured(self):
        self.start("modal_opener.htm")
        self.click_until("open_lookup", lambda: [
            w for w in self.window_list()
            if w["kind"] == "html_dialog" and "lookup dialog" in w["title"].lower()])
        self.wait_entry("modal_dialog.htm", method="GET")
        self.wait_entry("/api/lookup.xml?q=", method="GET")

    def test_load_time_error_captured(self):
        # errors.htm throws in window.onload: only the loader (proxy) can see that, the
        # post-call hook runs too late.
        self.start("errors.htm")

        def found():
            self.call("ie_text")
            return next((r for r in self.console_records()
                         if r["kind"] == "error" and "undefinedFunctionCall" in r["text"]), None)
        self.eventually(found, message="the load-time error record")

    def test_document_mode_unchanged_by_injection(self):
        # The spike's Q6: quirks.htm runs in mode 11 (BackCompat), not mode 5.
        expected = (("index.htm", 11), ("form.htm", 7), ("quirks.htm", 11),
                    ("headless.htm", 7), ("nomode.htm", 11))
        self.open_page(START_PAGE)
        for page, mode in expected:
            with self.subTest(page=page):
                self.text("ie_goto", url=self.base_url + page, wait=1)
                self.assertEqual(self.js("return document.documentMode"), mode)

    def test_late_charset_page_decodes(self):
        self.start("latecharset.htm")
        self.assertIn("Müller – café", self.text("ie_text"))
        self.assertEqual(self.js("return document.charset").lower(), "windows-1252")

    def test_request_body_decoded_with_page_charset_live(self):
        self.start("charset.htm")
        self.text("ie_fill", by="id", value="name", text="Müller – café")
        entry = self.click_until("send", self.post_entry)
        self.assertIn("Müller – café", self.text("ie_request", id=entry[0]))

    def test_network_clear_and_paging(self):
        self.start("index.htm")
        listed = self.entries()
        self.assertTrue(listed)
        last = listed[-1][0]
        # Paging: nothing at or below the last seen id comes back (a favicon may add newer ones).
        self.assertTrue(all(entry[0] > last for entry in self.entries(since_id=last)))
        self.assertTrue([e for e in listed if "index.htm" in e[4]])
        self.text("ie_network_clear")
        # The cleared entries are really gone (a favicon request may already have added a new one).
        after = self.entries()
        self.assertFalse([e for e in after if "index.htm" in e[4]], after)
        # A request made after the clear is listed, and ids kept counting: nothing old reappears.
        self.text("ie_goto", url=self.base_url + "form.htm", wait=1)
        new = self.wait_entry("form.htm", method="GET")
        self.assertGreater(new[0], last)
        self.assertTrue(all(entry[0] > last for entry in self.entries()))


if __name__ == "__main__":
    unittest.main()
