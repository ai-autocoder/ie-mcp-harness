"""Page instrumentation and the console tools in a real IE-mode window (plan Task 12).

What is recorded comes from ``instrument.js``, put into each page by the post-call hook (driver
documents) or the dialog drain (COM documents). Load-time errors of the first page are not
captured (spike D11: the initial URL is not proxied, and the loader only comes with the
proxy in Task 13); these tests therefore cause their events after the page has loaded.
Spike D5: ``console`` exists in documentMode 11 and not in documentMode 7.
"""
import json
import unittest

from ie_mcp.tools import console as consoletools
from tests.integration.base import IntegrationTestCase

LOOKUP = "lookup dialog"


class ConsoleTests(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(self.close_open_windows)

    # -- helpers -------------------------------------------------------------
    def records(self, **arguments):
        return json.loads(self.text("ie_console", **arguments))["records"]

    def js(self, script, **access):
        return json.loads(self.text("ie_js", script=script, **access))

    def find(self, kind, containing, drain="page", **filters):
        """The first record of ``kind`` whose text contains ``containing``, else None.

        ``drain`` makes the server read the page first: a driver call (post-call hook) for
        ``page``, a window listing (dialog drain, no driver) for ``windows``.
        """
        self.call("ie_text" if drain == "page" else "ie_windows")
        return next((r for r in self.records(**filters)
                     if r["kind"] == kind and containing in r["text"]), None)

    def wait_record(self, kind, containing, drain="page", **filters):
        return self.eventually(lambda: self.find(kind, containing, drain, **filters),
                               message=f"a {kind} record containing {containing!r}")

    def raise_dialog(self, button):
        """Click a button of the dialogs page (async, so the call cannot block) and wait for
        its script dialog window."""
        self.text("ie_click", by="id", value=button, mode="async", wait=0)
        return self.wait_window("script_dialog")

    # -- tests ---------------------------------------------------------------
    def test_late_error_and_console_captured(self):
        self.open_page("errors.htm")  # documentMode 11: has a console (D5)
        self.text("ie_click", by="id", value="late_error", wait=1)
        error = self.wait_record("error", "late-error")
        self.assertEqual(error["level"], "error")
        self.assertEqual(error["where"], "driver")
        self.assertEqual(self.js("return typeof window.console"), "object")
        self.js("console.log('fixture-log', 7); return 1")
        log = self.wait_record("console", "fixture-log 7", kinds=["console"])
        self.assertEqual(log["level"], "log")
        # the load-time error and log of the first page happened before anything was injected
        self.assertIsNone(self.find("error", "undefinedFunctionCall"))
        self.assertIsNone(self.find("error", "Object expected"))

    def test_alert_recorded(self):
        self.open_page("dialogs.htm")
        self.raise_dialog("do_alert")
        # The dialog-open record exists while the dialog is open: the call that listed the
        # windows drained the page through COM.
        opened = self.wait_record("dialog-open", "Hello alert", drain="windows")
        self.assertEqual(opened["level"], "alert")
        answer = json.loads(self.text("ie_dialog", action="accept"))
        self.assertEqual(answer["did"], "accepted")
        self.assertEqual(answer["evidence"]["kind"], "dialog-open")
        self.wait_no_window("script_dialog")
        closed = self.wait_record("dialog", "Hello alert")
        self.assertEqual(closed["level"], "alert")
        # The open record came in over COM and the close by the driver hook: the page-assigned
        # id pairs them, so nothing is pending any more and nothing could authorise an accept.
        self.assertNotEqual(opened["where"], closed["where"])
        self.assertEqual(opened["dialog_id"], closed["dialog_id"])
        self.assertIsNone(consoletools.STORE.pending_dialog())

    def test_second_dialog_of_a_page_is_accepted_with_the_pages_record(self):
        # IE shows the first dialog of a page as a MessageBox and a later one as a TaskDialog,
        # whose message cannot be read and which is accepted only with the page's own record.
        self.open_page("dialogs.htm")
        self.raise_dialog("do_alert")
        self.text("ie_dialog", action="accept")
        self.wait_no_window("script_dialog")
        confirm = self.raise_dialog("do_confirm")
        self.wait_record("dialog-open", "Confirm?", drain="windows")
        answer = json.loads(self.text("ie_dialog", target=confirm["target"], action="accept"))
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["kind"], "taskdialog")  # measured: the second dialog of a page
        self.assertEqual(answer["evidence"]["kind"], "dialog-open")
        self.wait_no_window("script_dialog")
        self.assertEqual(self.eventually(lambda: self.text("ie_get", by="id", value="confirm_result") or None,
                                         message="confirm_result to be filled"), "true")
        self.wait_record("dialog", "Confirm? -> true")
        # Used up and closed: a second TaskDialog would find no record to be accepted with.
        self.assertIsNone(consoletools.STORE.pending_dialog())
        self.assertIsNone(consoletools.STORE.take_pending_dialog())

    def test_modal_call_recorded_with_arguments(self):
        self.open_page("modal_opener.htm")
        self.text("ie_click", by="id", value="open_lookup", wait=0)
        dialog = self.wait_window("html_dialog", where=lambda w: LOOKUP in w["title"].lower())
        self.text("ie_wait_element", by="id", value="pick_A1", timeout=10, target=dialog["target"])
        self.text("ie_click", by="id", value="pick_A1", wait=1, target=dialog["target"])
        self.wait_no_window("html_dialog")
        modal = self.wait_record("modal", "modal_dialog.htm")
        self.assertIn("sCode=", modal["text"])
        self.assertIn("dialogWidth:420px", modal["text"])

    def test_dialog_records_drained(self):
        self.open_page("modal_opener.htm")
        self.text("ie_click", by="id", value="open_lookup", wait=0)
        dialog = self.wait_window("html_dialog", where=lambda w: LOOKUP in w["title"].lower())
        # The dialog is instrumented by the drain that follows every call while it is open; its
        # own alert then shows up in the records, tagged with the dialog's window.
        self.text("ie_wait_element", by="id", value="dialog_alert", timeout=10, target=dialog["target"])
        self.text("ie_click", by="id", value="dialog_alert", wait=0, target=dialog["target"])
        alert = self.wait_window("script_dialog")
        opened = self.wait_record("dialog-open", "Dialog alert", drain="windows")
        self.assertTrue(opened["where"].startswith("hwnd:"), opened)
        self.assertTrue(all(r["where"].startswith("hwnd:") for r in self.records()
                            if "Dialog alert" in r["text"]))
        self.text("ie_dialog", target=alert["target"], action="accept")
        self.wait_no_window("script_dialog")
        self.wait_record("dialog", "Dialog alert", drain="windows")
        self.text("ie_click", by="id", value="pick_A1", wait=1, target=dialog["target"])
        self.wait_no_window("html_dialog")

    def test_instrumentation_works_in_quirks_and_ie7(self):
        # quirks.htm runs in documentMode 11 (BackCompat) and form.htm in documentMode 7; the
        # instrumentation must not change either, nor leave anything in the DOM.
        for page, mode in (("quirks.htm", 11), ("form.htm", 7)):
            with self.subTest(page=page):
                self.open_page(page)
                self.assertEqual(self.js("return typeof window.__iemcp"), "object")
                self.assertEqual(self.js("return document.documentMode"), mode)
                html = self.text("ie_html")
                self.assertNotIn("__iemcp", html)
                self.assertEqual(self.js("return document.getElementsByTagName('script').length"),
                                 html.lower().count("<script"))
                self.assertEqual(self.js("return typeof window.__iemcp.take"), "function")

    def test_no_console_created_by_default(self):
        self.open_page("form.htm")  # documentMode 7: no console (D5)
        self.assertEqual(self.js("return typeof window.console"), "undefined")
        self.assertEqual(self.js("return typeof window.__iemcp"), "object")
        status = json.loads(self.text("ie_status"))
        self.assertTrue(status["instrumented"])
        self.assertFalse(status["console_stub"])

    def test_the_console_stub_and_the_off_switch(self):
        self.restart_session(IE_CONSOLE_STUB="1")
        self.open_page("form.htm")
        self.assertEqual(self.js("return typeof window.console"), "object")
        self.js("console.warn('stubbed'); return 1")
        self.assertEqual(self.wait_record("console", "stubbed")["level"], "warn")
        self.assertTrue(json.loads(self.text("ie_status"))["console_stub"])
        self.restart_session(IE_CONSOLE_STUB=None, IE_INSTRUMENT="0")
        self.open_page("form.htm")
        self.assertEqual(self.js("return typeof window.__iemcp"), "undefined")
        self.assertEqual(self.js("return typeof window.console"), "undefined")
        self.assertFalse(json.loads(self.text("ie_status"))["instrumented"])


if __name__ == "__main__":
    unittest.main()
