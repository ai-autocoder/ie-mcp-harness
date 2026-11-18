"""Reading and acting on fixture pages, through the driver and over COM, in a real IE-mode window.

Documents modes of the fixture pages (spike): index 11, form 7, quirks 11, headless 7, nomode 11.
"""
import json
import unittest

from ie_mcp.session import SESSION
from ie_mcp.win32 import htmldoc
from tests.integration.base import IntegrationTestCase

NON_ASCII = "Müller – café"


class ReadInteractTests(IntegrationTestCase):
    def com(self):
        """The ``target`` of the session's main window."""
        self.assertIsNotNone(SESSION.main_hwnd, "no main window was recorded for the session")
        return f"hwnd:{SESSION.main_hwnd}"

    # -- the driver ---------------------------------------------------------
    def test_frameset_read_nested_frame(self):
        self.open_page("frameset.htm")
        self.text("ie_wait_ready", timeout=30)
        self.assertIn("Inner frame", self.text("ie_text", frame="main/inner"))
        self.assertIn("Menu", self.text("ie_text", frame="menu"))

    def test_frame_tools_survive_frame_navigation(self):
        self.open_page("frameset.htm")
        self.text("ie_wait_ready", timeout=30)
        self.text("ie_click", by="id", value="menu_grid", frame="menu", wait=1)
        found = json.loads(self.text("ie_wait_text", contains="Gamma", frame="main", timeout=30))
        self.assertTrue(found["ok"], found)
        self.assertIn("Gamma", self.text("ie_text", frame="main"))

    def test_cross_origin_frame_does_not_break_tools(self):
        self.open_page("xorigin.htm")
        frames = json.loads(self.text("ie_frames"))
        self.assertEqual([f["name"] for f in frames], ["foreign", "local"])
        self.assertIn("Inner frame", self.text("ie_text", frame="local"))
        foreign = self.call("ie_text", frame="foreign")
        if foreign.get("isError"):
            self.assertIn("foreign", self.texts(foreign)[0])
        else:
            self.assertIn("Inner frame", self.texts(foreign)[0])
        self.assertTrue(json.loads(self.text("ie_status"))["alive"])
        ready = json.loads(self.text("ie_wait_ready", timeout=30))
        # Spike D13: a cross-origin frame denies script access, and the probe counts it.
        self.assertGreaterEqual(ready["cross_origin_frames"], 1, ready)

    def test_fill_select_submit_form(self):
        self.open_page("form.htm")
        self.text("ie_fill", by="id", value="code", text="A1")
        self.text("ie_select", by="id", value="kind", label="B", wait=0)
        self.text("ie_click", by="id", value="send", wait=1)
        self.text("ie_wait_element", by="id", value="echo_code", timeout=20)
        self.assertEqual(self.text("ie_get", by="id", value="echo_code"), "A1")
        self.assertEqual(self.text("ie_get", by="id", value="echo_kind"), "B")

    def test_fill_and_get_non_ascii(self):
        for page in ("charset.htm", "utf8.htm"):
            with self.subTest(page=page):
                self.open_page(page)
                self.text("ie_fill", by="id", value="name", text=NON_ASCII)
                self.assertEqual(self.text("ie_get", by="id", value="name", attr="value"), NON_ASCII)

    def test_quirks_page_readable(self):
        self.open_page("quirks.htm")
        self.assertIn("quirks ok", self.text("ie_text"))

    def test_grid_extraction(self):
        # grid.js starts with // comment lines: they must not swallow the code (driver and COM).
        self.open_page("grid.htm")
        found = json.loads(self.text("ie_wait_text", contains="Gamma", timeout=30))
        self.assertTrue(found["ok"], found)
        expected = [{"Code": "A1", "Name": "Alpha", "Price": "1.00"},
                    {"Code": "B2", "Name": "Beta", "Price": "2.50"},
                    {"Code": "C3", "Name": "Gamma", "Price": "10.00"}]
        for target in ({}, {"target": self.com()}):
            grid = json.loads(self.text("ie_grid", **target))
            self.assertEqual(grid["rows"], expected, target)
            self.assertEqual(grid["headers"], ["Code", "Name", "Price"])

    def test_frames_index_order_agrees_between_driver_and_com(self):
        self.open_page("frameset.htm")
        self.text("ie_wait_ready", timeout=30)
        listed = json.loads(self.text("ie_frames"))
        self.assertEqual([(f["index"], f["name"]) for f in listed], [(0, "menu"), (1, "main")])
        com = json.loads(self.text("ie_frames", target=self.com()))
        self.assertEqual(com, listed)
        for target in ({}, {"target": self.com()}):
            self.assertIn("Menu", self.text("ie_text", frame=0, **target), target)
            self.assertIn("Form", self.text("ie_text", frame=1, **target), target)
            self.assertIn("Form", self.text("ie_text", frame="1", **target), target)

    # -- COM ---------------------------------------------------------------
    def test_com_target_reads_main_window(self):
        self.open_page("index.htm")
        target = self.com()
        self.assertIn("Fixture index", self.text("ie_text", target=target))
        self.assertEqual(self.text("ie_js", target=target, script="return document.documentMode"),
                         "11")
        self.assertIn("<title>Fixture index</title>",
                      self.text("ie_html", target=target, limit=2000))
        with htmldoc.com_apartment():
            server = htmldoc.ie_server_child(SESSION.main_hwnd)
            self.assertIsNotNone(server, "the main window has no Internet Explorer_Server child")
            self.assertEqual(htmldoc.document_url(server), self.base_url + "index.htm")

        self.open_page("form.htm")
        self.assertEqual(self.text("ie_js", target=target, script="return document.documentMode"),
                         "7")
        self.text("ie_fill", target=target, by="id", value="code", text="Q7")
        self.assertEqual(self.text("ie_get", target=target, by="id", value="code", attr="value"),
                         "Q7")
        # The driver sees the value COM wrote, and the other way round.
        self.assertEqual(self.text("ie_get", by="id", value="code", attr="value"), "Q7")
        # (clear=False: on this machine a second WebDriver clear() of the same field fails with
        # "JavaScript error clearing the element", with or without COM; not a COM matter.)
        self.text("ie_fill", by="id", value="code", text="D1", clear=False)
        self.assertEqual(self.text("ie_get", target=target, by="id", value="code", attr="value"),
                         "Q7D1")
        self.text("ie_fill", target=target, by="id", value="code", text="over", clear=True)
        self.assertEqual(self.text("ie_get", by="id", value="code", attr="value"), "over")

    def test_com_tab_target_uses_the_ie_server_window(self):
        # D15: a tab is identified by its Internet Explorer_Server hwnd, which survives navigation.
        self.open_page("index.htm")
        with htmldoc.com_apartment():
            server = htmldoc.ie_server_child(SESSION.main_hwnd)
        self.assertIsNotNone(server)
        target = f"tab:{server}"
        self.assertIn("Fixture index", self.text("ie_text", target=target))
        self.open_page("form.htm")
        self.assertIn("Form", self.text("ie_text", target=target))
        gone = self.call("ie_text", target="tab:1")  # no such window
        self.assertTrue(gone.get("isError"), gone)
        self.assertIn("ie_attach", self.texts(gone)[0])

    def test_com_script_error_does_not_reach_the_page(self):
        self.open_page("index.htm")
        target = self.com()
        self.text("ie_js", script=("window.__errs = 0;"
                                   "window.onerror = function(){ window.__errs++; return true; };"
                                   "return 1;"))
        bad = self.call("ie_js", target=target, script="return (")
        self.assertTrue(bad.get("isError"), bad)
        thrown = self.call("ie_js", target=target, script="throw new Error('boom');")
        self.assertTrue(thrown.get("isError"), thrown)
        self.assertIn("boom", self.texts(thrown)[0])
        self.assertEqual(self.text("ie_js", script="return window.__errs"), "0")
        # The two documented expandos are all that is left, and they are emptied after each
        # request: __iemcp_r would otherwise keep page text readable by page script.
        self.text("ie_text", target=target)  # a request whose result is the page text
        state = self.text("ie_js", script=(
            "return [typeof window.__iemcp_e, String(window.__iemcp_e), typeof window.__iemcp_r,"
            " String(window.__iemcp_r)].join('|');"))
        self.assertEqual(json.loads(state), "string||object|null")
        names = self.text("ie_js", script=(
            "var n = []; for(var k in window){ if(k.indexOf('__iemcp') == 0){ n.push(k); } }"
            "n.sort(); return n.join(',');"))
        self.assertIn(json.loads(names), ("__iemcp_e,__iemcp_r", ""))

    def test_com_js_results_and_arguments(self):
        self.open_page("index.htm")
        target = self.com()
        self.assertEqual(self.text("ie_js", target=target, script="return 40 + 2"), "42")
        self.assertEqual(self.text("ie_js", target=target, script="return 'a' + 'b'"), '"ab"')
        self.assertEqual(self.text("ie_js", target=target, script="return [1, 2, 3]"),
                         json.dumps("1\x1e2\x1e3"))
        self.assertEqual(self.text("ie_js", target=target, script="return true"), "true")
        self.assertEqual(self.text("ie_js", target=target, script="return null"), "null")
        # Non-ASCII text and a closing script tag survive the round trip.
        echoed = self.text("ie_js", target=target, script="return 'ü </script> \\\\'")
        self.assertEqual(json.loads(echoed), "ü </script> \\")

    def test_com_frames_read_and_click(self):
        self.open_page("frameset.htm")
        self.text("ie_wait_ready", timeout=30)
        target = self.com()
        self.assertIn("Inner frame", self.text("ie_text", target=target, frame="main/inner"))
        self.text("ie_click", target=target, by="link_text", value="Grid", frame="menu", wait=1)
        found = json.loads(self.text("ie_wait_text", target=target, contains="Gamma",
                                     frame="main", timeout=30))
        self.assertTrue(found["ok"], found)
        self.assertIn("Gamma", self.text("ie_text", frame="main"))  # the driver agrees

    def test_com_cross_origin_frame_names_the_frame(self):
        self.open_page("xorigin.htm")
        self.text("ie_wait_ready", timeout=30)
        target = self.com()
        self.assertIn("Inner frame", self.text("ie_text", target=target, frame="local"))
        self.assertIn("Inner frame", self.text("ie_text", target=target, frame=1))
        foreign = self.call("ie_text", target=target, frame="foreign")
        self.assertTrue(foreign.get("isError"), foreign)
        self.assertIn("foreign", self.texts(foreign)[0])
        self.assertTrue(self.call("ie_text", target=target, frame=0).get("isError"))
        # The window is still usable afterwards.
        self.assertIn("Cross-origin frames", self.text("ie_text", target=target))
        self.assertTrue(json.loads(self.text("ie_status"))["alive"])

    def test_com_form_round_trip(self):
        self.open_page("form.htm")
        target = self.com()
        self.text("ie_fill", target=target, by="id", value="code", text="B2")
        self.text("ie_select", target=target, by="id", value="kind", label="C", wait=0)
        self.assertEqual(self.text("ie_get", target=target, by="id", value="kind", attr="value"),
                         "C")
        missing = self.call("ie_select", target=target, by="id", value="kind", label="Z", wait=0)
        self.assertTrue(missing.get("isError"), missing)
        self.assertIn("not found in id=kind", self.texts(missing)[0])
        self.text("ie_hover", target=target, by="id", value="code", wait=0)
        self.text("ie_scroll", target=target, by="id", value="send")
        self.text("ie_scroll", target=target, to="bottom")
        self.assertIn("queued", self.text("ie_click", target=target, by="id", value="send", wait=1))
        found = json.loads(self.text("ie_wait_element", target=target, by="id", value="echo_code",
                                     timeout=20))
        self.assertTrue(found["ok"], found)
        self.assertEqual(self.text("ie_get", target=target, by="id", value="echo_code"), "B2")
        self.assertEqual(self.text("ie_get", target=target, by="id", value="echo_kind"), "C")

    def test_com_locators(self):
        self.open_page("form.htm")
        target = self.com()
        for by, value in (("id", "code"), ("name", "code"), ("tag", "textarea"),
                          ("css", "#code")):
            with self.subTest(by=by):
                if by == "css":
                    # form.htm runs in documentMode 7: no querySelector there.
                    bad = self.call("ie_get", target=target, by=by, value=value, timeout=0)
                    self.assertTrue(bad.get("isError"))
                    self.assertIn("documentMode", self.texts(bad)[0])
                else:
                    self.text("ie_get", target=target, by=by, value=value, attr="id", timeout=0)
        xpath = self.call("ie_get", target=target, by="xpath", value="//a", timeout=0)
        self.assertTrue(xpath.get("isError"))
        self.assertIn("xpath is not supported", self.texts(xpath)[0])
        self.open_page("index.htm")
        self.assertEqual(self.text("ie_get", target=target, by="css", value="#typed", attr="id",
                                   timeout=0), "typed")

    def test_com_press_key_types_into_a_field(self):
        # D7: posted WM_KEYDOWN + WM_KEYUP gives one keypress and the character, in
        # documentMode 11 (index.htm) and 7 (form.htm).
        self.open_page("index.htm")
        target = self.com()
        self.text("ie_press_key", target=target, key="x", by="id", value="typed", wait=1)
        self.assertEqual(self.text("ie_get", by="id", value="keypress_count"), "1")
        self.assertEqual(self.text("ie_get", by="id", value="typed", attr="value"), "x")
        # An extended key (bit 24 of lParam): with no element given the keys go to the active
        # element (no re-focus), so Left moves the caret back one place before "y" is typed.
        self.text("ie_press_key", target=target, key="z", wait=1)
        self.text("ie_press_key", target=target, key="Left", wait=1)
        self.text("ie_press_key", target=target, key="y", wait=1)
        self.assertEqual(self.text("ie_get", by="id", value="typed", attr="value"), "xyz")
        self.open_page("form.htm")
        self.text("ie_press_key", target=target, key="q", by="id", value="code", wait=1)
        self.assertEqual(self.text("ie_get", by="id", value="code", attr="value"), "q")

    def test_com_wait_element_and_text(self):
        self.open_page("grid.htm")
        target = self.com()
        found = json.loads(self.text("ie_wait_text", target=target, contains="Gamma", timeout=30))
        self.assertTrue(found["ok"], found)
        gone = json.loads(self.text("ie_wait_text", target=target, contains="no such text here",
                                    timeout=2, interval=0.5))
        self.assertFalse(gone["ok"], gone)
        self.assertTrue(gone["timed_out"])
        absent = json.loads(self.text("ie_wait_element", target=target, by="id", value="nothing",
                                      timeout=1, interval=0.5))
        self.assertFalse(absent["ok"], absent)

    def test_no_false_modal_notice(self):
        # With no dialog open no result may carry a NOTICE item: the probe lists only visible
        # windows, because Edge's processes own hidden dialog-class windows.
        self.open_page("index.htm")
        results = [
            self.call("ie_text"), self.call("ie_frames"), self.call("ie_status"),
            self.call("ie_js", script="return 1"),
            self.call("ie_text", target=self.com()),
            self.call("ie_js", target=self.com(), script="return 1"),
        ]
        for result in results:
            self.assertFalse(result.get("isError"), result)
            for text in self.texts(result):
                self.assertFalse(text.startswith("NOTICE:"), text)
            self.assertEqual(len(self.texts(result)), 1, result)


if __name__ == "__main__":
    unittest.main()
