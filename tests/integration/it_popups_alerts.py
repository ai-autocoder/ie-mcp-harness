"""Script dialogs (alert, confirm, prompt) and ``window.open`` pop-ups in a real IE-mode window.

Spike decisions: D8 = the pop-up blocker does not stop an async click, so ``test_popup_window``
runs with mode ``driver`` and ``async``; D9 = the driver lists pop-ups, so ``ie_switch_window``
works; D14 = the driver alert reaches a prompt. Dialogs are opened with an async click: it
queues the click on the page's timer, so the call cannot block on the dialog it opens.
"""
import json
import unittest

from tests.integration.base import IntegrationTestCase


class PopupsAlertsTests(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(self.close_open_windows)

    # -- helpers -------------------------------------------------------------
    def fresh(self):
        self.close_open_windows()
        self.open_page("dialogs.htm")

    def raise_dialog(self, button, mode="async"):
        """Click a button of the dialogs page and wait for its script dialog window."""
        self.text("ie_click", by="id", value=button, mode=mode, wait=0)
        return self.wait_window("script_dialog")

    def dialog(self, **arguments):
        return json.loads(self.text("ie_dialog", **arguments))

    def shown(self, element_id):
        return self.text("ie_get", by="id", value=element_id)

    # -- alerts --------------------------------------------------------------
    def test_alert_accept_and_text(self):
        self.fresh()
        self.raise_dialog("do_alert")
        self.assertEqual(self.dialog(action="text")["text"], "Hello alert")
        self.assertEqual(self.dialog(action="accept")["did"], "accepted")
        self.wait_no_window("script_dialog")

    def test_alert_window_path_reads_text_and_accepts(self):
        # The same alert through its window (target hwnd): no driver call is needed.
        self.fresh()
        alert = self.raise_dialog("do_alert")
        self.assertEqual(self.dialog(target=alert["target"], action="text")["text"],
                         "Hello alert")
        self.assertEqual(self.dialog(target=alert["target"], action="accept")["did"], "accepted")
        self.wait_no_window("script_dialog")

    def test_alert_survives_screenshot_and_js(self):
        self.fresh()
        self.raise_dialog("do_alert")
        # Both calls may report the open alert (spike Q8); neither may dismiss it.
        self.call("ie_screenshot")
        self.call("ie_js", script="return 1")
        self.assertEqual(self.dialog(action="text")["text"], "Hello alert")
        self.dialog(action="accept")
        self.wait_no_window("script_dialog")

    # -- confirm -------------------------------------------------------------
    def test_confirm_dismiss(self):
        self.fresh()
        self.raise_dialog("do_confirm")
        self.assertEqual(self.dialog(action="text")["text"], "Confirm?")
        self.assertEqual(self.dialog(action="dismiss")["did"], "dismissed")
        self.assertEqual(self.eventually(lambda: self.shown("confirm_result") or None,
                                         message="confirm_result to be filled"), "false")

    def test_confirm_window_path_answers_true_and_false(self):
        # A confirm is a TaskDialog (spike Q8): accept is control 1 (true), dismiss 2 (false).
        for button, action, expected in (("do_confirm", "accept", "true"),
                                         ("do_confirm", "dismiss", "false")):
            with self.subTest(action=action):
                self.fresh()
                confirm = self.raise_dialog(button)
                answer = self.dialog(target=confirm["target"], action=action)
                self.assertTrue(answer["ok"], answer)
                self.wait_no_window("script_dialog")
                self.assertEqual(self.eventually(lambda: self.shown("confirm_result") or None,
                                                 message="confirm_result to be filled"), expected)

    # -- prompt --------------------------------------------------------------
    def test_prompt_answered_through_driver(self):
        self.fresh()
        self.raise_dialog("do_prompt")  # D14: a #32770 with an Edit; a script_dialog, no html_dialog
        windows = self.window_list()
        prompt = [w for w in windows if w["kind"] == "script_dialog"]
        self.assertEqual(len(prompt), 1, windows)
        self.assertTrue(prompt[0]["is_prompt"], prompt)
        self.assertFalse([w for w in windows if w["kind"] == "html_dialog"], windows)
        # The window path never answers a prompt: it cannot type into it.
        refused = self.call("ie_dialog", target=prompt[0]["target"], action="accept")
        self.assertTrue(refused.get("isError"), refused)
        self.assertIn("not a confirmed script dialog", self.texts(refused)[0])
        self.assertEqual(self.dialog(action="sendkeys", text="Ann")["did"], "sent+accepted")
        self.assertEqual(self.eventually(lambda: self.shown("prompt_result") or None,
                                         message="prompt_result to be filled"), "Ann")

    def test_prompt_closed_through_its_window_returns_null(self):
        self.fresh()
        prompt = self.raise_dialog("do_prompt")
        closed = json.loads(self.text("ie_close_window", target=prompt["target"]))
        self.assertTrue(closed["ok"], closed)
        self.wait_no_window("script_dialog")
        self.assertEqual(self.eventually(lambda: self.shown("prompt_result") or None,
                                         message="prompt_result to be filled"), "null")

    # -- pop-up window -------------------------------------------------------
    def test_popup_window(self):
        for mode in ("driver", "async"):  # D8: the pop-up blocker does not stop the async click
            with self.subTest(mode=mode):
                self.fresh()
                self.text("ie_click", by="id", value="do_popup", mode=mode, wait=0)
                popup = self.wait_window("popup")
                # Over COM, by hwnd ...
                self.assertIn("Popup window", self.text("ie_text", target=popup["target"]))
                # ... and through the driver (D9): switch, read, click close_me, switch back.
                switched = json.loads(self.text("ie_switch_window",
                                                title_contains="Fixture popup"))
                self.assertEqual(switched["title"], "Fixture popup")
                self.assertIn("Popup window", self.text("ie_text"))
                self.text("ie_click", by="id", value="close_me", wait=1)
                self.wait_no_window("popup")
                self.text("ie_switch_window", main=True)
                self.assertIn("Dialogs", self.text("ie_text"))

    def test_popup_closed_with_ie_close_window(self):
        self.fresh()
        self.text("ie_click", by="id", value="do_popup", mode="driver", wait=0)
        popup = self.wait_window("popup")
        closed = json.loads(self.text("ie_close_window", target=popup["target"]))
        self.assertTrue(closed["ok"], closed)
        self.wait_no_window("popup")
        # The main window is never closed this way.
        main = next(w for w in self.window_list() if w["kind"] == "main")
        refused = self.call("ie_close_window", target=main["target"])
        self.assertTrue(refused.get("isError"), refused)
        self.assertIn("ie_close", self.texts(refused)[0])


if __name__ == "__main__":
    unittest.main()
