"""Unit tests for the window and dialog tools (ie_windows, ie_switch_window, ie_close_window,
ie_dialog). Fakes only: no browser, no real windows, no COM and no pywin32.

Nothing here may ever send a message to a real window: ``messages.post_message`` is replaced
in every test and records what would have been sent.
"""
import contextlib
import dataclasses
import io
import json
import tempfile
import threading
import unittest
from unittest import mock

from ie_mcp import config, protocol, session
import ie_mcp.tools  # noqa: F401  (importing the package registers every tool)
from ie_mcp.backends import ComBackend
from ie_mcp.session import SESSION
from ie_mcp.tools import dialogs
from ie_mcp.tools import windows as wintools
from ie_mcp.win32 import messages, windows
from ie_mcp.win32.windows import RawChild, RawWindow, WindowInfo

REAL_ENUMERATE = windows.enumerate_windows  # before any test replaces it

EDGE_PID = 100
WM_COMMAND = messages.WM_COMMAND
WM_CLOSE = messages.WM_CLOSE

# Q8 shapes: (class, control ID) of every child of the dialog.
ALERT_CHILDREN = [RawChild(1, "Button", 2, True), RawChild(2, "Static", 20, True),
                  RawChild(3, "Static", 65535, True)]
TASKDIALOG_CHILDREN = [RawChild(1, "DirectUIHWND", 0, True)]
PROMPT_CHILDREN = [RawChild(1, "Button", 1, True), RawChild(2, "Button", 2, True),
                   RawChild(3, "Button", 57089, True), RawChild(4, "Edit", 8133, True),
                   RawChild(5, "Static", 8132, True), RawChild(6, "Static", 65535, True)]


def info(hwnd, kind, cls="Chrome_WidgetWin_1", title="", owner=None, visible=True, url="",
         is_prompt=False, owner_cls=None, owner_pid=EDGE_PID):
    """A WindowInfo. A script dialog is owned, by default, by the browser's dialog helper (D6)."""
    if owner_cls is None:
        owner_cls = windows.HELPER_CLASS if kind == "script_dialog" else ""
    return WindowInfo(hwnd=hwnd, pid=EDGE_PID, cls=cls, title=title, kind=kind,
                      owner_hwnd=owner, visible=visible, url=url, is_prompt=is_prompt,
                      owner_cls=owner_cls, owner_pid=owner_pid if owner_cls else None)


def alert_children(hwnd):
    """The Q8 alert children with their own hwnds (the message is the Static 65535)."""
    return [RawChild(hwnd + 1, "Button", 2, True), RawChild(hwnd + 2, "Static", 20, True),
            RawChild(hwnd + 3, "Static", 65535, True)]


def confirm_children(hwnd):
    """The classic OK/Cancel confirm: Button 1, Button 2, Static 20, Static 65535."""
    return [RawChild(hwnd + 1, "Button", 1, True), RawChild(hwnd + 2, "Button", 2, True),
            RawChild(hwnd + 3, "Static", 20, True), RawChild(hwnd + 4, "Static", 65535, True)]


def alert_info(hwnd, owner=None, **kwargs):
    return info(hwnd, "script_dialog", "#32770", "Message from webpage", owner, **kwargs)


def probe_entry(window):
    return {"hwnd": window.hwnd, "kind": window.kind, "title": window.title, "pid": EDGE_PID,
            "cls": window.cls, "owner_hwnd": window.owner_hwnd, "is_prompt": window.is_prompt}


def call(name, arguments=None):
    response = protocol.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": name, "arguments": arguments or {}}})
    return response["result"]


def first_text(result):
    return result["content"][0]["text"]


def is_error(result, *fragments):
    text = first_text(result)
    return bool(result.get("isError")) and all(f in text for f in fragments)


class FakeSwitch:
    def __init__(self, driver):
        self.driver = driver

    def window(self, handle):
        if handle not in self.driver.pages:
            raise RuntimeError(f"no such window: {handle}")
        self.driver.current = handle
        self.driver.calls.append(("switch", handle))

    @property
    def alert(self):
        self.driver.alert_reads += 1
        if self.driver.alert_obj is None:
            raise RuntimeError("no alert open")
        return self.driver.alert_obj


class FakeDriver:
    """A driver with windows ``{handle: (title, url)}``; the first handle is current."""

    def __init__(self, pages):
        self.pages = dict(pages)
        self.current = next(iter(self.pages))
        self.calls = []
        self.alert_reads = 0
        self.alert_obj = None
        self.switch_to = FakeSwitch(self)

    @property
    def window_handles(self):
        return list(self.pages)

    @property
    def current_window_handle(self):
        return self.current

    @property
    def title(self):
        return self.pages[self.current][0]

    @property
    def current_url(self):
        return self.pages[self.current][1]

    def close(self):
        self.calls.append(("close", self.current))
        del self.pages[self.current]


class WindowToolsTestCase(unittest.TestCase):
    def setUp(self):
        stderr = contextlib.redirect_stderr(io.StringIO())
        stderr.__enter__()
        self.addCleanup(stderr.__exit__, None, None, None)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        original = config.SETTINGS
        paths = config.build_session_paths(env={"IE_SESSION_ROOT": tmp.name}, pid=1)
        config.SETTINGS = dataclasses.replace(original, session_paths=paths, tool_timeout=5)
        self.addCleanup(setattr, config, "SETTINGS", original)
        for name in ("MODAL_PROBE", "POST_HOOKS", "AFTER_CALL", "CALL_CONTEXTS"):
            self.addCleanup(setattr, protocol, name, getattr(protocol, name))
        protocol.POST_HOOKS, protocol.AFTER_CALL, protocol.CALL_CONTEXTS = [], [], []
        protocol.MODAL_PROBE = lambda: []

        self.infos = []        # what enumerate_windows reports
        self.children = {}     # hwnd -> list of RawChild
        self.texts = {}        # hwnd -> control text
        self.posted = []       # (hwnd, msg, wparam, lparam) the tools would have sent
        self.gone = True       # is_window is False; a test sets it False to keep a window alive
        self.enumerate_calls = []

        def enumerate_windows(pids, enum_fn=None, with_url=False, attached_tops=None):
            self.enumerate_calls.append({"with_url": with_url, "pids": set(pids)})
            return list(self.infos)

        self.patch(windows, "enumerate_windows", enumerate_windows)
        self.patch(windows, "session_window_pids", lambda: {EDGE_PID})
        self.patch(windows, "enum_children", lambda hwnd: list(self.children.get(hwnd, [])))
        def post_message(hwnd, msg, wparam=0, lparam=0):
            self.posted.append((hwnd, msg, wparam, lparam))
            self.gone = True  # a posted close/answer makes the window go away
            return True

        self.patch(messages, "post_message", post_message)
        self.patch(messages, "is_window", lambda hwnd: not self.gone)
        self.patch(messages, "get_text", lambda hwnd: self.texts.get(hwnd, ""))
        self.patch(windows, "ATTACHED_TOPS", set())
        self.patch(wintools, "SWITCH_WAIT", 0)  # a selector that matches nothing is an error at once
        self.patch(SESSION, "main_hwnd", None)
        self.patch(SESSION, "driver", None)
        # A unit test must never start a real browser: SESSION.ensure() would (this leaked
        # IEDriver + Edge processes in a red run before the tools existed).
        self.patch(SESSION, "ensure", mock.Mock(side_effect=AssertionError(
            "the test started a browser through SESSION.ensure()")))

    def patch(self, target, name, value):
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def set_modal(self, *windows_):
        protocol.MODAL_PROBE = lambda: [probe_entry(w) for w in windows_]

    def use_driver(self, driver):
        self.patch(SESSION, "driver", driver)
        self.patch(SESSION, "ensure", lambda *a, **k: driver)
        return driver

    def dialog(self, window, children):
        """Register an enumerated dialog window and its child controls."""
        self.infos.append(window)
        self.children[window.hwnd] = children
        return window


class WindowsToolTests(WindowToolsTestCase):
    def test_windows_tool_lists_dialog_with_target(self):
        self.infos = [info(10, "main", title="Page", url="http://x/"),
                      info(42, "html_dialog", "Internet Explorer_TridentDlgFrame", "Lookup",
                           owner=9, url="http://x/dlg"),
                      info(43, "other")]
        self.set_modal(self.infos[1])
        result = call("ie_windows")
        self.assertFalse(result.get("isError"), result)
        items = json.loads(first_text(result))
        by_hwnd = {i["hwnd"]: i for i in items}
        self.assertEqual(by_hwnd[42]["kind"], "html_dialog")
        self.assertEqual(by_hwnd[42]["target"], "hwnd:42")
        self.assertEqual(by_hwnd[42]["url"], "http://x/dlg")
        self.assertNotIn(43, by_hwnd)  # windows that are neither pages nor dialogs are left out
        self.assertTrue(self.enumerate_calls[0]["with_url"])
        self.assertEqual(self.enumerate_calls[0]["pids"], {EDGE_PID})

    def test_windows_tool_works_while_driver_locked(self):
        self.infos = [info(10, "main", title="Page")]
        driver = self.use_driver(FakeDriver({"H1": ("Page", "http://x/")}))
        held, release = threading.Event(), threading.Event()

        def holder():
            with protocol.driver_hold("other_tool", 1) as got:
                assert got
                held.set()
                release.wait(10)

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(release.set)
        self.assertTrue(held.wait(5))
        result = call("ie_windows")
        self.assertFalse(result.get("isError"), result)
        item = json.loads(first_text(result))[0]
        self.assertEqual(item["target"], "hwnd:10")
        self.assertNotIn("driver_handle", item)  # a busy lock simply omits the driver fields
        self.assertEqual(driver.calls, [])

    def test_windows_tool_skips_driver_while_modal_open(self):
        dialog = info(42, "html_dialog", "Internet Explorer_TridentDlgFrame", "Lookup")
        self.infos = [info(10, "main", title="Page"), dialog]
        self.set_modal(dialog)

        class Untouchable:
            def __getattr__(self, name):
                raise AssertionError(f"the driver was used: {name}")

        self.patch(SESSION, "driver", Untouchable())
        items = json.loads(first_text(call("ie_windows")))
        self.assertEqual(len(items), 2)
        for item in items:
            self.assertNotIn("driver_handle", item)
            self.assertNotIn("current", item)

    def test_windows_tool_matches_driver_handles_by_title_then_url(self):
        self.infos = [info(10, "main", title="Page - Edge", url="http://x/"),
                      info(11, "popup", title="Same - Edge", url="http://x/pop2"),
                      info(12, "popup", title="Same - Edge", url="http://x/pop1")]
        driver = self.use_driver(FakeDriver({
            "H1": ("Page", "http://x/"), "H2": ("Same", "http://x/pop1"),
            "H3": ("Same", "http://x/pop2")}))
        items = {i["hwnd"]: i for i in json.loads(first_text(call("ie_windows")))}
        self.assertEqual(items[10]["driver_handle"], "H1")
        self.assertTrue(items[10]["current"])
        self.assertEqual(items[12]["driver_handle"], "H2")
        self.assertEqual(items[11]["driver_handle"], "H3")
        self.assertFalse(items[11]["current"])
        self.assertEqual(driver.current, "H1")  # always switched back to where it was

    def test_windows_tool_survives_unreadable_url(self):
        # enumerate_windows leaves "" when COM rejects the read; the tool still answers.
        self.infos = [info(10, "main", title="Page", url="")]
        items = json.loads(first_text(call("ie_windows")))
        self.assertEqual(items[0]["url"], "")


class SwitchWindowTests(WindowToolsTestCase):
    def setUp(self):
        super().setUp()
        self.infos = [info(10, "main", title="Page - Edge", url="http://x/"),
                      info(11, "popup", title="Popup - Edge", url="http://x/pop")]
        self.patch(SESSION, "main_hwnd", 10)
        self.driver = self.use_driver(FakeDriver({
            "H1": ("Page", "http://x/"), "H2": ("Popup", "http://x/pop")}))

    def test_switch_by_handle_returns_title_and_url(self):
        result = call("ie_switch_window", {"handle": "H2"})
        self.assertFalse(result.get("isError"), result)
        got = json.loads(first_text(result))
        self.assertEqual((got["handle"], got["title"], got["url"]),
                         ("H2", "Popup", "http://x/pop"))
        self.assertEqual(self.driver.current, "H2")

    def test_switch_by_title_and_main(self):
        call("ie_switch_window", {"title_contains": "pop"})  # case-insensitive
        self.assertEqual(self.driver.current, "H2")
        got = json.loads(first_text(call("ie_switch_window", {"main": True})))
        self.assertEqual(got["handle"], "H1")
        self.assertEqual(self.driver.current, "H1")

    def test_switch_needs_exactly_one_selector(self):
        for args in ({}, {"handle": "H2", "main": True}, {"handle": "H2", "title_contains": "p"}):
            self.assertTrue(is_error(call("ie_switch_window", args), "exactly one"), args)

    def test_switch_unknown_handle_or_title_keeps_current_window(self):
        self.assertTrue(is_error(call("ie_switch_window", {"handle": "nope"}), "nope"))
        self.assertTrue(is_error(call("ie_switch_window", {"title_contains": "zzz"}), "zzz"))
        self.assertEqual(self.driver.current, "H1")


class CloseWindowTests(WindowToolsTestCase):
    def test_close_window_hwnd_prefers_com_then_wm_close(self):
        self.infos = [info(10, "main"), info(20, "popup", title="Popup")]
        closes = []
        with mock.patch.object(ComBackend, "close_window",
                               lambda self_: closes.append(self_.hwnd)):
            result = call("ie_close_window", {"target": "hwnd:20"})
        self.assertFalse(result.get("isError"), result)
        self.assertEqual(closes, [20])
        self.assertEqual(self.posted, [])  # COM worked: no message was needed
        # COM fails (no document, rejected call): the window is asked to close with WM_CLOSE.
        self.gone = False  # the window is still there
        with mock.patch.object(ComBackend, "close_window",
                               side_effect=RuntimeError("COM call failed")):
            result = call("ie_close_window", {"target": "hwnd:20"})
        self.assertFalse(result.get("isError"), result)
        self.assertEqual(self.posted, [(20, WM_CLOSE, 0, 0)])

    def test_close_window_com_error_on_a_window_that_is_already_gone_is_success(self):
        # The close worked and COM lost the window mid-call: nothing to post to, nothing wrong.
        self.infos = [info(10, "main"), info(20, "popup", title="Popup")]
        self.gone = True
        with mock.patch.object(ComBackend, "close_window",
                               side_effect=RuntimeError("COM call failed (Member not found)")):
            result = call("ie_close_window", {"target": "hwnd:20"})
        self.assertFalse(result.get("isError"), result)
        self.assertTrue(json.loads(first_text(result))["closed"])
        self.assertEqual(self.posted, [])

    def test_close_window_refuses_main(self):
        self.infos = [info(10, "main")]
        with mock.patch.object(ComBackend, "close_window") as com:
            self.assertTrue(is_error(call("ie_close_window", {"target": "hwnd:10"}), "ie_close"))
        com.assert_not_called()
        self.assertEqual(self.posted, [])

    def test_close_window_refuses_attached_top_and_unknown_windows(self):
        self.infos = [info(30, "popup")]
        self.patch(windows, "ATTACHED_TOPS", {30})
        self.assertTrue(is_error(call("ie_close_window", {"target": "hwnd:30"}), "ie_detach"))
        self.assertTrue(is_error(call("ie_close_window", {"target": "hwnd:999"}),
                                 "not a window of this session"))
        self.assertEqual(self.posted, [])

    def test_system_dialog_never_clicked(self):
        self.dialog(info(50, "system_dialog", "#32770", "Open"), [RawChild(1, "Button", 1, True)])
        self.set_modal(self.infos[0])
        for name, args in (("ie_close_window", {"target": "hwnd:50"}),
                           ("ie_dialog", {"target": "hwnd:50", "action": "accept"}),
                           ("ie_dialog", {"target": "hwnd:50", "action": "dismiss"})):
            with self.subTest(tool=name, args=args):
                self.assertTrue(is_error(call(name, args), "ask the user"))
        self.assertEqual(self.posted, [])

    def test_close_window_on_script_dialog_applies_the_shape_check(self):
        self.dialog(alert_info(60), ALERT_CHILDREN)
        self.dialog(alert_info(61), ALERT_CHILDREN + [RawChild(9, "Edit", 1, True)])
        self.dialog(info(62, "script_dialog", "#32770", "needs some information",
                         is_prompt=True), PROMPT_CHILDREN)
        self.assertFalse(call("ie_close_window", {"target": "hwnd:60"}).get("isError"))
        self.assertEqual(self.posted, [(60, WM_COMMAND, 2, 0)])
        self.posted.clear()
        self.assertTrue(is_error(call("ie_close_window", {"target": "hwnd:61"}),
                                 "not a confirmed script dialog"))
        self.assertEqual(self.posted, [])
        # A prompt (its full Q8 child set) may be closed: Cancel is control 2. So may the
        # first prompt of a page, which has no "don't create more messages" check box.
        self.assertFalse(call("ie_close_window", {"target": "hwnd:62"}).get("isError"))
        self.assertEqual(self.posted, [(62, WM_COMMAND, 2, 0)])
        self.posted.clear()
        self.dialog(info(63, "script_dialog", "#32770", "needs some information",
                         is_prompt=True), [c for c in PROMPT_CHILDREN if c.ctrl_id != 57089])
        self.assertFalse(call("ie_close_window", {"target": "hwnd:63"}).get("isError"))
        self.assertEqual(self.posted, [(63, WM_COMMAND, 2, 0)])

    def test_close_window_by_driver_handle_returns_to_main(self):
        self.infos = [info(10, "main", title="Page - Edge", url="http://x/"),
                      info(11, "popup", title="Popup - Edge", url="http://x/pop")]
        self.patch(SESSION, "main_hwnd", 10)
        driver = self.use_driver(FakeDriver({
            "H1": ("Page", "http://x/"), "H2": ("Popup", "http://x/pop")}))
        result = call("ie_close_window", {"target": "H2"})
        self.assertFalse(result.get("isError"), result)
        self.assertIn(("close", "H2"), driver.calls)
        self.assertEqual(driver.current, "H1")
        self.assertTrue(is_error(call("ie_close_window", {"target": "H1"}), "ie_close"))
        self.assertTrue(is_error(call("ie_close_window", {"target": "nope"}), "nope"))

    def test_close_window_by_driver_handle_refuses_when_main_cannot_be_told(self):
        # Two handles, none of which matches the main window: guessing could close the real
        # main window or refuse the real pop-up, so nothing is closed.
        self.infos = [info(10, "main", title="Other - Edge", url="http://x/")]
        self.patch(SESSION, "main_hwnd", 10)
        driver = self.use_driver(FakeDriver({
            "H1": ("Page", "http://x/"), "H2": ("Popup", "http://x/pop")}))
        result = call("ie_close_window", {"target": "H2"})
        self.assertTrue(is_error(result, "cannot tell which driver handle is the main"), result)
        self.assertEqual([c for c in driver.calls if c[0] == "close"], [])
        self.assertEqual(driver.current, "H1")
        self.assertTrue(is_error(call("ie_switch_window", {"main": True}),
                                 "cannot tell which driver window is the main"))

    def test_a_sole_driver_handle_is_the_main_window(self):
        self.infos = [info(10, "main", title="Page - Edge", url="http://x/")]
        self.use_driver(FakeDriver({"H1": ("Page", "http://x/")}))
        got = json.loads(first_text(call("ie_switch_window", {"main": True})))
        self.assertEqual(got["handle"], "H1")


class DialogToolTests(WindowToolsTestCase):
    def test_dialog_hwnd_target_needs_no_driver(self):
        spec = protocol.REGISTRY["ie_dialog"]
        self.assertFalse(spec.needs_driver({"target": "hwnd:1"}))
        self.assertFalse(spec.needs_driver({"target": "driver"}))  # refused by the tool itself
        self.assertTrue(spec.needs_driver({}))

    def test_dialog_hwnd_target_alert_text_accept_dismiss(self):
        self.dialog(alert_info(70, owner=9), alert_children(70))
        self.texts[73] = "Hello alert"  # the Static 65535 child
        text = json.loads(first_text(call("ie_dialog", {"target": "hwnd:70", "action": "text"})))
        self.assertEqual(text["text"], "Hello alert")
        self.assertEqual(self.posted, [])
        for action in ("accept", "dismiss"):  # both close an alert with control 2 (Q8)
            self.posted.clear()
            result = call("ie_dialog", {"target": "hwnd:70", "action": action})
            self.assertFalse(result.get("isError"), result)
            self.assertEqual(self.posted, [(70, WM_COMMAND, 2, 0)])
            self.assertEqual(json.loads(first_text(result))["text"], "Hello alert")

    def test_dialog_hwnd_target_classic_confirm_reads_text_and_answers(self):
        self.dialog(info(80, "script_dialog", "#32770", "Message from webpage"),
                    confirm_children(80))
        self.texts[84] = "Confirm?"
        said = json.loads(first_text(call("ie_dialog", {"target": "hwnd:80", "action": "text"})))
        self.assertEqual((said["text"], said["kind"]), ("Confirm?", "confirm"))
        call("ie_dialog", {"target": "hwnd:80", "action": "accept"})
        call("ie_dialog", {"target": "hwnd:80", "action": "dismiss"})
        self.assertEqual(self.posted, [(80, WM_COMMAND, 1, 0), (80, WM_COMMAND, 2, 0)])

    def test_dialog_hwnd_target_taskdialog_answers_but_has_no_text(self):
        # The later dialogs of a page are TaskDialogs (one DirectUIHWND child): an alert and a
        # confirm look the same, OK is control 1, Cancel 2, and the message is not readable.
        self.dialog(info(81, "script_dialog", "#32770", "Message from webpage"),
                    TASKDIALOG_CHILDREN)
        call("ie_dialog", {"target": "hwnd:81", "action": "accept"})
        call("ie_dialog", {"target": "hwnd:81", "action": "dismiss"})
        self.assertEqual(self.posted, [(81, WM_COMMAND, 1, 0), (81, WM_COMMAND, 2, 0)])
        text = json.loads(first_text(call("ie_dialog", {"target": "hwnd:81", "action": "text"})))
        self.assertIsNone(text["text"])  # no driver, nothing invented
        self.assertIn("not readable", text["note"])

    def test_dialog_hwnd_target_refuses_unconfirmed(self):
        outside = info(90, "system_dialog", "#32770", "Save as")      # owner outside the session
        odd = alert_info(91, owner=9)  # an extra Edit control
        prompt = info(92, "script_dialog", "#32770", "x needs some information",
                      is_prompt=True)
        other = info(93, "popup")
        # A script_dialog-shaped window whose owner is outside the session PIDs (and outside
        # the attached tabs), whatever the classification said.
        foreign = alert_info(95, owner=9, owner_pid=9999)
        for window, children in ((outside, [RawChild(1, "Button", 2, True)]),
                                 (odd, ALERT_CHILDREN + [RawChild(9, "Edit", 5, True)]),
                                 (prompt, PROMPT_CHILDREN), (other, []),
                                 (foreign, alert_children(95))):
            self.dialog(window, children)
        for window in (outside, odd, prompt, other, foreign, info(94, "main")):
            with self.subTest(hwnd=window.hwnd):
                result = call("ie_dialog", {"target": f"hwnd:{window.hwnd}", "action": "accept"})
                self.assertTrue(is_error(result, "not a confirmed script dialog"), result)
        self.assertEqual(self.posted, [])

    def test_dialog_prompt_not_reachable(self):
        prompt = self.dialog(info(100, "script_dialog", "#32770", "x needs some information",
                                  is_prompt=True), PROMPT_CHILDREN)
        self.set_modal(prompt)
        driver = self.use_driver(FakeDriver({"H1": ("Page", "http://x/")}))
        with mock.patch.object(dialogs, "PROMPT_DRIVER_REACH", False):  # D14: driver reach = no
            result = call("ie_dialog", {})
        self.assertTrue(is_error(result, "prompt not reachable: ask the user"), result)
        self.assertEqual(driver.alert_reads, 0)
        self.assertEqual(self.posted, [])

    def test_dialog_refuses_non_hwnd_targets(self):
        lock = mock.MagicMock()
        self.patch(protocol, "DRIVER_LOCK", lock)
        ensure = mock.MagicMock()
        self.patch(SESSION, "ensure", ensure)
        for target in ("tab:5", "driver", "x", "hwnd:", "hwnd:0", ""):
            with self.subTest(target=target):
                self.assertTrue(is_error(call("ie_dialog", {"target": target})), target)
        lock.acquire.assert_not_called()
        ensure.assert_not_called()
        self.assertEqual(self.posted, [])

    def test_dialog_skips_driver_alert_while_html_dialog_open(self):
        frame = info(110, "html_dialog", "Internet Explorer_TridentDlgFrame", "Lookup")
        alert = alert_info(111, owner=110)
        self.infos = [frame]
        self.dialog(alert, alert_children(111))
        self.texts[114] = "Dialog alert"
        self.set_modal(frame, alert)
        driver = self.use_driver(FakeDriver({"H1": ("Page", "http://x/")}))
        result = call("ie_dialog", {})
        self.assertFalse(result.get("isError"), result)
        self.assertEqual(driver.alert_reads, 0)
        self.assertEqual(self.posted, [(111, WM_COMMAND, 2, 0)])
        self.assertEqual(json.loads(first_text(result))["did"], "accepted")

    def test_dialog_prompt_inside_modal_cannot_be_answered(self):
        frame = info(120, "html_dialog", "Internet Explorer_TridentDlgFrame", "Lookup")
        prompt = self.dialog(info(121, "script_dialog", "#32770", "x needs some information",
                                  owner=120, is_prompt=True), PROMPT_CHILDREN)
        self.infos.append(frame)
        self.set_modal(frame, prompt)
        driver = self.use_driver(FakeDriver({"H1": ("Page", "http://x/")}))
        result = call("ie_dialog", {})
        self.assertTrue(is_error(result, "prompt not reachable: ask the user"), result)
        self.assertEqual(driver.alert_reads, 0)
        self.assertEqual(self.posted, [])

    def test_dialog_without_target_uses_driver_alert_first(self):
        class Alert:
            text = "Hello alert"
            accepted = 0

            def accept(self):
                Alert.accepted += 1

        driver = self.use_driver(FakeDriver({"H1": ("Page", "http://x/")}))
        driver.alert_obj = Alert()
        answer = json.loads(first_text(call("ie_dialog", {})))
        self.assertEqual((answer["ok"], answer["did"], answer["text"]),
                         (True, "accepted", "Hello alert"))
        self.assertEqual(Alert.accepted, 1)
        self.assertEqual(self.posted, [])

    def test_dialog_without_target_and_nothing_open(self):
        self.use_driver(FakeDriver({"H1": ("Page", "http://x/")}))
        answer = json.loads(first_text(call("ie_dialog", {})))
        self.assertFalse(answer["ok"])
        self.assertIn("no dialog present", answer["note"])


SHAPES = {"classic alert": alert_children, "classic confirm": confirm_children,
          "taskdialog": lambda hwnd: [RawChild(hwnd + 1, "DirectUIHWND", 0, True)]}


class OwnerConfirmationTests(WindowToolsTestCase):
    """A #32770 owned by the browser's top window (a system dialog the page caused, or a
    MessageBox) has the same shapes as a script dialog; only its owner tells them apart."""

    def test_script_dialog_shapes_with_a_browser_window_owner_are_refused(self):
        for label, children in SHAPES.items():
            for owner_cls in ("Chrome_WidgetWin_1", windows.IE_SERVER_CLASS, "Shell_TrayWnd"):
                with self.subTest(shape=label, owner=owner_cls):
                    self.infos = [alert_info(200, owner=1, owner_cls=owner_cls)]
                    self.children = {200: children(200)}
                    for name, args in (("ie_dialog", {"target": "hwnd:200", "action": "accept"}),
                                       ("ie_dialog", {"target": "hwnd:200", "action": "dismiss"}),
                                       ("ie_close_window", {"target": "hwnd:200"})):
                        result = call(name, args)
                        self.assertTrue(is_error(result, "not a confirmed script dialog"), result)
                        self.assertIn("ask the user", first_text(result))
                    self.assertEqual(self.posted, [])

    def test_the_helper_and_the_dialog_frame_own_real_script_dialogs(self):
        for owner_cls in (windows.HELPER_CLASS, windows.DIALOG_FRAME_CLASS):
            with self.subTest(owner=owner_cls):
                self.posted.clear()
                self.infos = [alert_info(201, owner=1, owner_cls=owner_cls)]
                self.children = {201: alert_children(201)}
                result = call("ie_dialog", {"target": "hwnd:201", "action": "accept"})
                self.assertFalse(result.get("isError"), result)
                self.assertEqual(self.posted, [(201, WM_COMMAND, 2, 0)])

    def test_a_helper_of_another_process_is_not_the_sessions(self):
        self.infos = [alert_info(202, owner=1, owner_cls=windows.HELPER_CLASS, owner_pid=9999)]
        self.children = {202: alert_children(202)}
        self.assertTrue(is_error(call("ie_dialog", {"target": "hwnd:202"}),
                                 "not a confirmed script dialog"))
        self.assertEqual(self.posted, [])


class RealEnumerationTests(WindowToolsTestCase):
    """The same refusals through the real ``enumerate_windows`` (fake raw windows), so the
    classification and the owner fields are exercised too, not only the tool."""

    def setUp(self):
        super().setUp()
        self.patch(windows, "enumerate_windows", REAL_ENUMERATE)
        ie_server = [RawChild(5000, windows.IE_SERVER_CLASS, 0, True)]
        self.raw = {
            1: RawWindow(1, EDGE_PID, "Chrome_WidgetWin_1", "Page", None, True, ie_server),
            2: RawWindow(2, EDGE_PID, windows.HELPER_CLASS, "", None, True, []),
            3: RawWindow(3, 9, windows.HELPER_CLASS, "", None, True, []),
        }
        self.patch(windows, "_enum_top_level", lambda: list(self.raw.values()))

    def dialog_owned_by(self, hwnd, owner):
        self.raw[hwnd] = RawWindow(hwnd, EDGE_PID, "#32770", "Message from webpage", owner,
                                   True, alert_children(hwnd))
        self.children[hwnd] = alert_children(hwnd)

    def test_owner_outside_the_session_is_refused(self):
        self.dialog_owned_by(10, owner=3)  # owned by a helper of ANOTHER process
        result = call("ie_dialog", {"target": "hwnd:10", "action": "accept"})
        self.assertTrue(is_error(result, "not a confirmed script dialog"), result)
        self.assertTrue(is_error(call("ie_close_window", {"target": "hwnd:10"}), "ask the user"))
        self.assertEqual(self.posted, [])

    def test_owner_is_the_browser_main_window_is_refused(self):
        # The owner is in the session, so the classification says script_dialog; the owner
        # class (not the helper or a dialog frame) is what refuses it.
        self.dialog_owned_by(11, owner=1)
        self.assertEqual(windows.find_window(11).kind, "script_dialog")
        self.assertTrue(is_error(call("ie_dialog", {"target": "hwnd:11", "action": "accept"}),
                                 "not a confirmed script dialog"))
        self.assertTrue(is_error(call("ie_close_window", {"target": "hwnd:11"}),
                                 "not a confirmed script dialog"))
        self.assertEqual(self.posted, [])

    def test_owner_is_the_session_helper_is_answered(self):
        self.dialog_owned_by(12, owner=2)
        result = call("ie_dialog", {"target": "hwnd:12", "action": "accept"})
        self.assertFalse(result.get("isError"), result)
        self.assertEqual(self.posted, [(12, WM_COMMAND, 2, 0)])


class TaskDialogTextTests(WindowToolsTestCase):
    def test_taskdialog_text_is_not_read_through_the_driver_while_a_modal_html_dialog_is_open(self):
        frame = info(300, "html_dialog", "Internet Explorer_TridentDlgFrame", "Lookup")
        dialog = self.dialog(alert_info(301, owner=300),
                             [RawChild(302, "DirectUIHWND", 0, True)])
        self.infos.append(frame)
        self.set_modal(frame, dialog)
        driver = self.use_driver(FakeDriver({"H1": ("Page", "http://x/")}))
        text = json.loads(first_text(call("ie_dialog", {"target": "hwnd:301", "action": "text"})))
        self.assertIsNone(text["text"])
        self.assertIn("not readable", text["note"])
        self.assertEqual(driver.alert_reads, 0)

    def test_taskdialog_text_comes_from_the_driver_when_it_is_the_only_dialog(self):
        class Alert:
            text = "Later alert"

        dialog = self.dialog(alert_info(310), [RawChild(311, "DirectUIHWND", 0, True)])
        self.set_modal(dialog)
        driver = self.use_driver(FakeDriver({"H1": ("Page", "http://x/")}))
        driver.alert_obj = Alert()
        text = json.loads(first_text(call("ie_dialog", {"target": "hwnd:310", "action": "text"})))
        self.assertEqual(text["text"], "Later alert")


class MessageSmokeTests(unittest.TestCase):
    """Real ctypes prototypes (64-bit handles); read-only, sends nothing to any window."""

    def test_is_window_and_get_text_on_no_window(self):
        self.assertFalse(messages.is_window(0))
        self.assertFalse(messages.is_window(0x7FFFFFF0))
        self.assertEqual(messages.get_text(0), "")

    def test_is_window_true_for_a_real_top_level_window(self):
        top = next(iter(windows._enum_top_level()), None)
        self.assertIsNotNone(top, "the desktop has no top-level window")
        self.assertTrue(messages.is_window(top.hwnd))


class ChildrenSmokeTests(unittest.TestCase):
    def test_enum_children_are_the_direct_children_only(self):
        # Real ctypes (GetParent prototype), read-only: every direct child is also a descendant.
        for top in list(windows._enum_top_level())[:40]:
            descendants = {c.hwnd for c in windows.enum_descendants(top.hwnd)}
            children = {c.hwnd for c in windows.enum_children(top.hwnd)}
            self.assertLessEqual(children, descendants)


class NoticeTextTests(unittest.TestCase):
    def test_notice_names_the_d2_paths_first(self):
        text = protocol._notice({"kind": "html_dialog", "title": "Lookup", "hwnd": 42})
        self.assertLess(text.index("ie_switch_window"), text.index("ie_windows"))
        self.assertIn('target="hwnd:42"', text)


if __name__ == "__main__":
    unittest.main()
