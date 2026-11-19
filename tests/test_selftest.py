"""The ``--selftest`` report and the verdicts of its checks (no browser)."""
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

from ie_mcp import cli

TRIDENT = "Mozilla/5.0 (Windows NT 10.0; WOW64; Trident/7.0; rv:11.0) like Gecko"
CHROMIUM = "Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 Chrome/120.0 Safari/537.36 Edg/120.0"


def run(report_fn):
    out = io.StringIO()
    with redirect_stdout(out):
        value = report_fn()
    return value, out.getvalue().splitlines()


class ReportTests(unittest.TestCase):
    def test_lines_and_result_pass(self):
        report = cli.Report()
        _, lines = run(lambda: (report.add(cli.PASS, "a", "x"), report.add(cli.SKIP, "b"),
                                report.result()))
        self.assertEqual(lines, ["  PASS  a: x", "  SKIP  b", "RESULT: PASS"])

    def test_a_failure_fails_the_run(self):
        report = cli.Report()
        code, lines = run(lambda: (report.add(cli.FAIL, "a", "broken"), report.result())[1])
        self.assertEqual(code, 1)
        self.assertEqual(lines[-1], "RESULT: FAIL")

    def test_pass_exit_code_is_zero(self):
        report = cli.Report()
        code, _ = run(report.result)
        self.assertEqual(code, 0)

    def test_run_turns_an_exception_into_a_failure(self):
        report = cli.Report()

        def boom():
            raise RuntimeError("no driver\nsecond line")
        status, lines = run(lambda: report.run("session", boom))
        self.assertEqual(status, cli.FAIL)
        self.assertEqual(lines, ["  FAIL  session: no driver"])
        self.assertEqual(run(report.result)[0], 1)

    def test_run_passes_a_verdict_through(self):
        report = cli.Report()
        status, lines = run(lambda: report.run("x", lambda: (cli.SKIP, "why")))
        self.assertEqual((status, lines), (cli.SKIP, ["  SKIP  x: why"]))


class VerdictTests(unittest.TestCase):
    def test_session_needs_trident_and_document_mode_7(self):
        self.assertEqual(cli.session_verdict(TRIDENT, 7)[0], cli.PASS)
        self.assertEqual(cli.session_verdict(TRIDENT, 11)[0], cli.FAIL)
        self.assertEqual(cli.session_verdict(CHROMIUM, 7)[0], cli.FAIL)
        self.assertEqual(cli.session_verdict(None, None)[0], cli.FAIL)

    def test_proxy_verdict(self):
        self.assertEqual(cli.proxy_verdict(False, "off", "")[0], cli.SKIP)
        self.assertEqual(cli.proxy_verdict(True, "proxy", "")[0], cli.PASS)
        for state in ("page-only", "unverified"):
            status, detail = cli.proxy_verdict(True, state, "the note")
            self.assertEqual(status, cli.FAIL)
            self.assertIn("the note", detail)

    def test_loader_verdict(self):
        self.assertEqual(cli.loader_verdict("page-only", True, True, 7)[0], cli.SKIP)
        self.assertEqual(cli.loader_verdict("off", True, True, 7)[0], cli.SKIP)
        self.assertEqual(cli.loader_verdict("proxy", False, True, 7)[0], cli.SKIP)
        self.assertEqual(cli.loader_verdict("proxy", True, True, 7)[0], cli.PASS)
        self.assertEqual(cli.loader_verdict("proxy", True, True, 11)[0], cli.FAIL)
        # No evidence that the loader ran: never a PASS, whatever the document mode is.
        self.assertEqual(cli.loader_verdict("proxy", True, False, 7)[0], cli.FAIL)

    def test_instrumentation_verdict(self):
        self.assertEqual(cli.instrumentation_verdict(False, "undefined", False)[0], cli.SKIP)
        self.assertEqual(cli.instrumentation_verdict(True, "object", True)[0], cli.PASS)
        self.assertEqual(cli.instrumentation_verdict(True, "undefined", True)[0], cli.FAIL)
        self.assertEqual(cli.instrumentation_verdict(True, "object", False)[0], cli.FAIL)


class StaticChecksTests(unittest.TestCase):
    """Which missing dependency stops the browser checks: selenium, driver and Edge do; pywin32 not."""

    def run_static(self, driver=sys.executable, edge=sys.executable, pywin32=True):
        settings = SimpleNamespace(driver_path=driver, edge_path=edge)
        modules = {} if pywin32 else {"pythoncom": None}  # None makes the import raise
        report = cli.Report()
        with mock.patch.dict(sys.modules, modules):
            value, lines = run(lambda: cli._static_checks(report, settings))
        return value, lines

    def test_all_present(self):
        self.assertEqual(self.run_static(pywin32=False)[0][0], True)

    def test_missing_pywin32_fails_its_line_but_allows_the_browser_checks(self):
        (can_browse, has_pywin32), lines = self.run_static(pywin32=False)
        self.assertTrue(can_browse)
        self.assertFalse(has_pywin32)
        self.assertTrue(any(line.startswith("  FAIL  pywin32") for line in lines), lines)

    def test_missing_driver_or_edge_stops_the_browser_checks(self):
        for kwargs in ({"driver": None}, {"driver": "C:/no/such/IEDriverServer.exe"},
                       {"edge": "C:/no/such/msedge.exe"}):
            with self.subTest(**kwargs):
                (can_browse, _), _ = self.run_static(**kwargs)
                self.assertFalse(can_browse)


class ModalCheckTests(unittest.TestCase):
    """_modal_check with a scripted tool: a late dialog, a click that errors, no dialog at all."""

    DIALOG = {"kind": "html_dialog", "target": "hwnd:7", "title": "Lookup"}

    def check(self, dialog_after, click_errors=False, closes=True):
        """``dialog_after``: the ie_windows call (1-based) from which the dialog is listed
        (None: never); it disappears after ie_close_window when ``closes``."""
        state = {"windows": 0, "clicks": 0, "closed": False, "calls": []}

        def tool(name, recorded, **arguments):
            state["calls"].append(name)
            if name == "ie_click":
                state["clicks"] += 1
                if click_errors:
                    raise RuntimeError("ie_click: BLOCKED: modal dialog open")
            elif name == "ie_close_window":
                state["closed"] = closes
            elif name == "ie_windows":
                state["windows"] += 1
                shown = (dialog_after is not None and state["windows"] >= dialog_after
                         and not state["closed"])
                return [json.dumps([{"kind": "main", "target": "hwnd:1"}]
                                   + ([self.DIALOG] if shown else []))]
            return ["ok"]
        try:
            result = cli._modal_check("http://x/", [], tool=tool, sleep=lambda s: None)
        except RuntimeError as exc:
            result = exc
        return result, state

    def test_dialog_appears_at_once(self):
        result, state = self.check(dialog_after=2)
        self.assertEqual(result[0], cli.PASS)
        self.assertEqual(state["clicks"], 1)

    def test_a_late_dialog_is_not_clicked_over(self):
        # Listed only after the first poll window (24 polls) of the first click: a second
        # click would hit a modal dialog and fail with BLOCKED.
        result, state = self.check(dialog_after=1 + 24 + 1)
        self.assertEqual(result[0], cli.PASS)
        self.assertEqual(state["clicks"], 1)

    def test_a_click_error_still_looks_for_the_dialog(self):
        result, state = self.check(dialog_after=3, click_errors=True)
        self.assertEqual(result[0], cli.PASS)

    def test_no_dialog_is_one_clear_failure(self):
        result, state = self.check(dialog_after=None, click_errors=True)
        self.assertIsInstance(result, RuntimeError)
        self.assertIn("no modal dialog showed up", str(result))
        self.assertIn("BLOCKED", str(result))  # the click error is named, not raised
        self.assertEqual(state["clicks"], 3)

    def test_a_dialog_that_does_not_close_is_a_failure(self):
        result, _ = self.check(dialog_after=2, closes=False)
        self.assertIsInstance(result, RuntimeError)
        self.assertIn("still open", str(result))


if __name__ == "__main__":
    unittest.main()
