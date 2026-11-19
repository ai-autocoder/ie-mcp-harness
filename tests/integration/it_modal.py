"""showModalDialog windows in a real IE-mode window, reached both ways (spike D2 = both).

Every test runs once per path, with ``subTest``:

* ``driver``: ``ie_switch_window`` to the dialog's driver handle, act with the plain tools,
  ``ie_switch_window main=true`` afterwards;
* ``com``: the same tools with ``target="hwnd:<n>"`` (the dialog's ``Internet Explorer_TridentDlgFrame``).

Each sub-test ends with no dialog open and the driver back on the main window.
"""
import json
import time
import unittest

from ie_mcp import config
from tests.integration.base import IntegrationTestCase

PATHS = ("driver", "com")
LOOKUP, CONFIRM, NESTED = "lookup dialog", "confirm dialog", "nested dialog"  # title parts


class ModalTests(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(self.close_open_windows)

    # -- helpers -------------------------------------------------------------
    def fresh_opener(self):
        """A clean state: nothing open, the driver on the main window, the opener page loaded."""
        self.close_open_windows()
        self.open_page("modal_opener.htm")

    def dialog_window(self, part, timeout=10):
        return self.wait_window("html_dialog", timeout, where=lambda w: part in w["title"].lower())

    def open_dialog(self, button, part):
        """Click an opener button (the default click mode) and wait for the dialog's window."""
        self.text("ie_click", by="id", value=button, wait=0)
        return self.dialog_window(part)

    def access(self, path, dialog, part):
        """The ``target`` arguments for acting inside ``dialog`` by ``path``."""
        if path == "driver":
            self.text("ie_switch_window", title_contains=part)
            return {}
        return {"target": dialog["target"]}

    def back_to_main(self, path):
        if path == "driver":
            self.text("ie_switch_window", main=True)

    def value(self, element_id, **access):
        return self.text("ie_get", by="id", value=element_id, attr="value", **access)

    def shown(self, element_id, **access):
        return self.text("ie_get", by="id", value=element_id, **access)

    # -- tests ---------------------------------------------------------------
    def test_lookup_dialog_roundtrip(self):
        for path in PATHS:
            with self.subTest(path=path):
                self.fresh_opener()
                dialog = self.open_dialog("open_lookup", LOOKUP)
                access = self.access(path, dialog, LOOKUP)
                self.text("ie_click", by="id", value="pick_A1", wait=1, **access)
                self.wait_no_window("html_dialog")
                self.back_to_main(path)
                self.assertEqual(self.eventually(lambda: self.value("lookup_code") or None,
                                                 message="lookup_code to be filled"), "A1")
                self.assertEqual(self.value("lookup_name"), "Alpha")

    def test_lookup_dialog_grid_readable(self):
        for path in PATHS:
            with self.subTest(path=path):
                self.fresh_opener()
                dialog = self.open_dialog("open_lookup", LOOKUP)
                access = self.access(path, dialog, LOOKUP)
                self.text("ie_wait_element", by="id", value="pick_C3", timeout=10, **access)
                grid = json.loads(self.text("ie_grid", **access))
                self.assertEqual([row["Code"] for row in grid["rows"]], ["A1", "B2", "C3"], grid)
                self.back_to_main(path)
                self.close_open_windows()

    def test_lookup_dialog_key_enter(self):
        for path in PATHS:
            with self.subTest(path=path):
                self.fresh_opener()
                dialog = self.open_dialog("open_lookup", LOOKUP)
                access = self.access(path, dialog, LOOKUP)
                self.text("ie_wait_element", by="id", value="pick_C3", timeout=10, **access)
                self.text("ie_fill", by="id", value="search", text="B", **access)
                self.text("ie_press_key", key="Enter", by="id", value="search", wait=1, **access)
                self.eventually(lambda: self.shown("search_count", **access) == "1",
                                message="search_count to become 1")
                self.back_to_main(path)
                self.close_open_windows()

    def test_confirm_dialog_return_value(self):
        for path in PATHS:
            with self.subTest(path=path):
                self.fresh_opener()
                dialog = self.open_dialog("ask", CONFIRM)
                access = self.access(path, dialog, CONFIRM)
                self.text("ie_click", by="id", value="yes", wait=1, **access)
                self.wait_no_window("html_dialog")
                self.back_to_main(path)
                self.assertEqual(self.eventually(lambda: self.value("answer") or None,
                                                 message="answer to be filled"), "yes")

    def test_nested_modal(self):
        for path in PATHS:
            with self.subTest(path=path):
                self.fresh_opener()
                lookup = self.open_dialog("open_lookup", LOOKUP)
                access = self.access(path, lookup, LOOKUP)
                self.text("ie_wait_element", by="id", value="pick_B2", timeout=10, **access)
                self.text("ie_click", by="id", value="nested", wait=0, **access)
                nested = self.dialog_window(NESTED)
                self.assertNotEqual(nested["hwnd"], lookup["hwnd"])
                nested_access = self.access(path, nested, NESTED)
                self.text("ie_click", by="id", value="nested_ok", wait=1, **nested_access)
                self.eventually(lambda: not [w for w in self.window_list()
                                             if NESTED in w["title"].lower()],
                                message="the nested dialog to close")
                # Back in the lookup dialog, which is still open and received the result.
                access = self.access(path, lookup, LOOKUP)
                self.assertEqual(self.eventually(
                    lambda: self.shown("nested_result", **access) or None,
                    message="nested_result to be filled"), "nested-ok")
                self.text("ie_click", by="id", value="pick_B2", wait=1, **access)
                self.wait_no_window("html_dialog")
                self.back_to_main(path)
                self.assertEqual(self.eventually(lambda: self.value("lookup_code") or None,
                                                 message="lookup_code to be filled"), "B2")

    def test_alert_inside_dialog(self):
        for path in PATHS:
            with self.subTest(path=path):
                self.fresh_opener()
                dialog = self.open_dialog("open_lookup", LOOKUP)
                access = self.access(path, dialog, LOOKUP)
                self.text("ie_click", by="id", value="dialog_alert", wait=0, **access)
                # D6: an alert inside a showModalDialog window is owned by that window.
                alert = self.wait_window("script_dialog",
                                         where=lambda w: w["owner_hwnd"] == dialog["hwnd"])
                said = json.loads(self.text("ie_dialog", target=alert["target"], action="text"))
                self.assertEqual(said["text"], "Dialog alert")
                answered = json.loads(self.text("ie_dialog", target=alert["target"],
                                                action="accept"))
                self.assertEqual(answered["did"], "accepted")
                self.wait_no_window("script_dialog")
                self.assertEqual(self.eventually(
                    lambda: self.shown("dialog_alert_result", **access) or None,
                    message="dialog_alert_result to be filled"), "alerted")
                self.back_to_main(path)
                self.close_open_windows()

    def test_driver_click_on_modal_opener_does_not_hang_server(self):
        self.fresh_opener()
        # The click may return at once (spike Q1) or only when the dialog closes: either way
        # the server must keep answering. Do not assert on the click's own result.
        self.call("ie_click", by="id", value="open_lookup", mode="driver", wait=0)
        limit = config.SETTINGS.tool_timeout + 5
        dialog = None
        deadline = time.monotonic() + limit
        while dialog is None:
            started = time.monotonic()
            windows = self.window_list()  # one ie_windows call, timed on its own
            took = time.monotonic() - started
            self.assertLess(took, limit, f"ie_windows took {took:.1f}s with a dialog open")
            dialog = next((w for w in windows if w["kind"] == "html_dialog"
                           and LOOKUP in w["title"].lower()), None)
            if dialog is None:
                self.assertLess(time.monotonic(), deadline, "the dialog never showed up")
                time.sleep(0.25)
        closed = json.loads(self.text("ie_close_window", target=dialog["target"]))
        self.assertTrue(closed["ok"], closed)
        self.wait_no_window("html_dialog")
        self.assertEqual(self.value("lookup_code"), "")  # the driver works again


if __name__ == "__main__":
    unittest.main()
