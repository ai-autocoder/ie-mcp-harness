"""Attaching to a tab (plan Task 15), against the session's own browser window.

The window ``ie_open`` made is the stand-in for "a tab the user opened": ``ie_tabs`` lists it and
``ie_attach`` drives it over COM, with no driver tool involved. Network capture is off, so no
proxy loader is inserted: what ends up in the tab comes from the attach itself.
"""
import json
import unittest
from unittest import mock

from ie_mcp import backends
from ie_mcp.win32 import windows
from tests.integration.base import IntegrationTestCase


class AttachTests(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(self.call, "ie_detach")  # first to run: the teardown's ie_close needs it

    def tabs(self):
        return json.loads(self.text("ie_tabs"))

    def tab_of(self, part):
        tab = next((t for t in self.tabs() if part in t["url"]), None)
        self.assertIsNotNone(tab, f"no tab with {part!r} in its URL: {self.tabs()}")
        return tab

    def test_attach_reads_ie_mode_tab(self):
        self.restart_session(IE_CAPTURE="0")
        self.open_page("form.htm")
        tab = self.tab_of("form.htm")
        attached = json.loads(self.text("ie_attach", hwnd=tab["hwnd"]))
        self.assertEqual(attached["attached"], f"tab:{tab['hwnd']}")
        self.assertEqual(backends.DEFAULT_TARGET, f"tab:{tab['hwnd']}")
        self.assertIn("Inner frame", self.text("ie_text", frame="inner"))
        # a tool that needs the driver is refused, and the page is still the attached one
        refused = self.call("ie_wait_ready")
        self.assertTrue(refused.get("isError"), refused)
        self.assertIn("ie_detach", self.texts(refused)[0])
        self.text("ie_click", by="id", value="send", wait=1)
        found = json.loads(self.text("ie_wait_text", contains="Echo", timeout=20))
        self.assertTrue(found["ok"], found)
        # the new document got the instrumentation from the drain after that call
        self.text("ie_js", script="window.setTimeout(function(){"
                                  "throw new Error('tab-late-error');},0);return 1")
        self.text("ie_sleep", seconds=1)
        records = json.loads(self.text("ie_console", kinds=["error"]))["records"]
        error = next((r for r in records if "tab-late-error" in r["text"]), None)
        self.assertIsNotNone(error, records)
        self.assertTrue(error["where"].startswith("tab:"), error)
        detached = json.loads(self.text("ie_detach"))
        self.assertTrue(detached["ok"])
        self.assertIsNone(backends.DEFAULT_TARGET)
        self.assertEqual(windows.ATTACHED_TOPS, set())

    def test_attach_sees_and_answers_an_alert_of_the_tab(self):
        self.restart_session(IE_CAPTURE="0")
        self.open_page("dialogs.htm")
        tab = self.tab_of("dialogs.htm")
        self.text("ie_attach", hwnd=tab["hwnd"])
        # Only the attach may vouch for the dialog: the session's own process list is empty.
        patcher = mock.patch.object(windows, "session_window_pids", lambda: set())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertIn("queued", self.text("ie_click", by="id", value="do_alert", mode="async",
                                          wait=0))
        alert = self.wait_window("script_dialog")
        self.assertEqual(self.eventually(
            lambda: [w for w in windows.modal_probe() if w["hwnd"] == alert["hwnd"]],
            message="the alert in the modal probe")[0]["kind"], "script_dialog")
        answer = json.loads(self.text("ie_dialog", target=alert["target"], action="accept"))
        self.assertEqual(answer["did"], "accepted")
        self.wait_no_window("script_dialog")

    def test_ie_attach_refuses_a_window_that_is_not_a_listed_tab(self):
        self.open_page("form.htm")
        result = self.call("ie_attach", hwnd=1)
        self.assertTrue(result.get("isError"), result)
        self.assertIsNone(backends.DEFAULT_TARGET)


if __name__ == "__main__":
    unittest.main()
