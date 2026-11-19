"""Unit tests for attaching to a tab the user opened (plan Task 15). Fakes only: no browser, no
real windows, no COM. Nothing here may start a browser (``SESSION.ensure`` raises)."""
import contextlib
import dataclasses
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from ie_mcp import backends, config, protocol, session
import ie_mcp.tools  # noqa: F401  (importing the package registers every tool)
from ie_mcp.session import SESSION
from ie_mcp.tools import attach, console as consoletools
from ie_mcp.win32 import dialogs as scriptdialogs
from ie_mcp.win32 import htmldoc, messages, processes, windows
from ie_mcp.win32.windows import RawChild, RawWindow, TabInfo, WindowInfo

IE_SERVER = "Internet Explorer_Server"
EDGE = 100          # the user's Edge process
OTHER_SESSION = 200  # another ie-mcp session's Edge process


def raw(hwnd, pid=EDGE, cls="Chrome_WidgetWin_1", owner=None, title="", visible=True,
        children=()):
    return RawWindow(hwnd=hwnd, pid=pid, cls=cls, title=title, owner=owner, visible=visible,
                     children=list(children))


def alert_window(hwnd, owner):
    return raw(hwnd, cls="#32770", owner=owner, title="Message from webpage", children=[
        RawChild(hwnd + 1, "Button", 2, True), RawChild(hwnd + 2, "Static", 20, True),
        RawChild(hwnd + 3, "Static", 65535, True)])


def call(name, arguments=None):
    response = protocol.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": name, "arguments": arguments or {}}})
    return response["result"]


def text_of(result):
    return result["content"][0]["text"]


class AttachTestCase(unittest.TestCase):
    def setUp(self):
        stderr = contextlib.redirect_stderr(io.StringIO())
        stderr.__enter__()
        self.addCleanup(stderr.__exit__, None, None, None)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        original = config.SETTINGS
        paths = config.build_session_paths(env={"IE_SESSION_ROOT": tmp.name}, pid=1)
        config.SETTINGS = dataclasses.replace(original, session_paths=paths, tool_timeout=5)
        self.addCleanup(setattr, config, "SETTINGS", original)
        for name in ("MODAL_PROBE", "POST_HOOKS", "AFTER_CALL", "CALL_CONTEXTS"):
            self.addCleanup(setattr, protocol, name, getattr(protocol, name))
        protocol.POST_HOOKS, protocol.AFTER_CALL, protocol.CALL_CONTEXTS = [], [], []
        protocol.MODAL_PROBE = windows.modal_probe
        for owner, name in ((backends, "DEFAULT_TARGET"), (backends, "ATTACHED_CHAINS"),
                            (windows, "ATTACHED_TOPS"), (windows, "ATTACHED_PIDS")):
            self.addCleanup(setattr, owner, name, getattr(owner, name))
        backends.DEFAULT_TARGET, backends.ATTACHED_CHAINS = None, {}
        windows.ATTACHED_TOPS, windows.ATTACHED_PIDS = set(), frozenset()
        self.patch(SESSION, "edge_pid", None)
        self.patch(SESSION, "main_hwnd", None)
        self.patch(SESSION, "driver", None)
        self.ensure = mock.Mock(side_effect=AssertionError("the test started a browser"))
        self.patch(SESSION, "ensure", self.ensure)
        self.patch(processes, "snapshot", lambda fresh=False: [
            processes.ProcessRow(EDGE, 1, "msedge.exe"),
            processes.ProcessRow(OTHER_SESSION, 1, "msedge.exe"),
            processes.ProcessRow(300, 1, "notepad.exe")])
        self.patch(windows, "other_session_pids", lambda: set())
        self.patch(windows, "other_sessions_starting", lambda: 0)
        self.patch(windows, "window_pid", lambda hwnd: EDGE)
        self.patch(windows, "ancestor_chain", lambda hwnd: [hwnd - 1])
        self.patch(htmldoc, "document_url", lambda hwnd: "http://127.0.0.1/form.htm")
        self.patch(windows, "session_window_pids", lambda: set())
        self.patch(consoletools, "drain_target", lambda target: 0)
        self.patch(messages, "is_window", lambda hwnd: True)
        self.patch(attach, "USED", False)
        self.patch(attach, "_probe_tab", lambda target: "http://127.0.0.1/form.htm")

    def patch(self, target, name, value):
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tab(self, hwnd=11, top=10, pid=EDGE):
        return TabInfo(hwnd=hwnd, top_hwnd=top, ancestors=[top], pid=pid, title="Form - Edge",
                       url="http://127.0.0.1/form.htm")


class IeModeTabsTests(AttachTestCase):
    def test_ie_mode_tabs_finds_windows_with_ie_server(self):
        fake = [
            raw(10, title="Form - Edge", children=[RawChild(11, IE_SERVER, 0, True)]),
            raw(20, title="no IE page", children=[RawChild(21, "Chrome_RenderWidgetHostHWND", 0, True)]),
            raw(30, title="hidden window", visible=False, children=[RawChild(31, IE_SERVER, 0, True)]),
            raw(40, title="inactive tab", children=[RawChild(41, IE_SERVER, 0, False)]),
            raw(50, pid=300, title="not Edge", children=[RawChild(51, IE_SERVER, 0, True)]),
        ]
        tabs = windows.ie_mode_tabs(enum_fn=lambda: fake)
        self.assertEqual(len(tabs), 1)
        tab = tabs[0]
        self.assertEqual((tab.hwnd, tab.top_hwnd, tab.pid), (11, 10, EDGE))
        self.assertEqual(tab.ancestors, [10])
        self.assertEqual((tab.title, tab.url), ("Form - Edge", "http://127.0.0.1/form.htm"))

    def test_ie_mode_tabs_url_is_empty_when_com_is_missing(self):
        def no_com(hwnd):
            raise RuntimeError("pywin32 is required")
        self.patch(htmldoc, "document_url", no_com)
        tabs = windows.ie_mode_tabs(enum_fn=lambda: [
            raw(10, children=[RawChild(11, IE_SERVER, 0, True)])])
        self.assertEqual([t.url for t in tabs], [""])

    def test_ie_mode_tabs_excludes_other_live_sessions(self):
        self.patch(windows, "other_session_pids", lambda: {OTHER_SESSION})
        fake = [
            raw(10, children=[RawChild(11, IE_SERVER, 0, True)]),
            raw(60, pid=OTHER_SESSION, children=[RawChild(61, IE_SERVER, 0, True)]),
        ]
        self.assertEqual([t.hwnd for t in windows.ie_mode_tabs(enum_fn=lambda: fake)], [11])

    def test_ie_mode_tabs_excludes_a_server_owned_by_another_sessions_process(self):
        self.patch(windows, "other_session_pids", lambda: {OTHER_SESSION})
        self.patch(windows, "window_pid", lambda hwnd: OTHER_SESSION if hwnd == 11 else EDGE)
        fake = [raw(10, children=[RawChild(11, IE_SERVER, 0, True)])]
        self.assertEqual(windows.ie_mode_tabs(enum_fn=lambda: fake), [])

    def test_other_session_pids_reads_live_owner_records_only(self):
        root = self.tmp / "sessions"
        records = {"mine": (7, 100, "mine"), "live": (8, 200, "live"), "dead": (9, 400, "dead"),
                   "starting": (8, None, "starting")}
        for name, (owner, edge, session_id) in records.items():
            (root / name).mkdir(parents=True)
            (root / name / "owner.json").write_text(json.dumps(
                {"session_id": session_id, "owner_pid": owner, "edge_pid": edge}))
        (root / "live" / "owner.stale-5.json").write_text(json.dumps(
            {"session_id": "live", "owner_pid": 5, "edge_pid": 500}))
        self.assertEqual(session.live_edge_roots(root, "mine", pid_alive=lambda pid: pid == 8),
                         [200])

    def test_ancestor_chain_of_a_top_window_is_empty(self):
        with mock.patch.object(windows, "root_window", lambda hwnd: hwnd):
            self.assertEqual(REAL_ANCESTOR_CHAIN(5), [])

    def test_ancestor_chain_walks_get_parent_up_to_the_top_window(self):
        # 5 is a child of 4, which is a child of the top window 3; the top's own GetParent is its
        # owner (99), which must not be followed.
        fake = mock.Mock()
        fake.GetParent.side_effect = lambda hwnd: {5: 4, 4: 3, 3: 99}.get(hwnd, 0)
        with mock.patch.object(windows, "_user32", lambda: fake),                 mock.patch.object(windows, "root_window", lambda hwnd: 3):
            self.assertEqual(REAL_ANCESTOR_CHAIN(5), [4, 3])
            self.assertEqual(REAL_ANCESTOR_CHAIN(4), [3])

    def test_starting_sessions_counts_only_marked_live_sessions_without_an_edge_pid(self):
        root = self.tmp / "sessions"
        now = 1000.0
        records = {
            "mine": {"session_id": "mine", "owner_pid": 7, "starting_since": now},
            "starting": {"session_id": "starting", "owner_pid": 8, "starting_since": now - 5},
            "idle": {"session_id": "idle", "owner_pid": 8},
            "started": {"session_id": "started", "owner_pid": 8, "starting_since": now - 5,
                        "edge_pid": 500},
            "old mark": {"session_id": "old", "owner_pid": 8, "starting_since": now - 1000},
            "dead": {"session_id": "dead", "owner_pid": 9, "starting_since": now - 5},
        }
        for name, record in records.items():
            (root / name).mkdir(parents=True)
            (root / name / "owner.json").write_text(json.dumps(record))
        self.assertEqual(session.starting_sessions(root, "mine", lambda pid: pid == 8, now), 1)


REAL_ANCESTOR_CHAIN = windows.ancestor_chain


class AttachToolTests(AttachTestCase):
    def modal_world(self):
        # Edge window 10 (attached) and 20 (the user's other window), both in the same process;
        # an alert owned by each.
        fake = [raw(10, children=[RawChild(11, IE_SERVER, 0, True)]), raw(20),
                alert_window(30, owner=10), alert_window(40, owner=20)]
        self.patch(windows, "_enum_top_level", lambda: list(fake))

    def test_attach_sets_and_detach_clears_default_target(self):
        self.modal_world()
        self.patch(windows, "ie_mode_tabs", lambda enum_fn=None: [self.tab()])
        self.assertEqual(windows.modal_probe(), [])
        result = call("ie_attach", {"hwnd": 11})
        self.assertFalse(result.get("isError"), result)
        self.assertEqual(backends.DEFAULT_TARGET, "tab:11")
        self.assertEqual(backends.ATTACHED_CHAINS, {11: [10]})
        self.assertEqual(windows.ATTACHED_TOPS, {10})
        self.assertEqual(windows.ATTACHED_PIDS, {EDGE})
        # the dialog the attached window owns is a modal; the other Edge window's is not
        self.assertEqual([w["hwnd"] for w in windows.modal_probe()], [30])
        detached = call("ie_detach")
        self.assertFalse(detached.get("isError"), detached)
        self.assertIn("reload", text_of(detached))
        self.assertIsNone(backends.DEFAULT_TARGET)
        self.assertEqual(backends.ATTACHED_CHAINS, {})
        self.assertEqual(windows.ATTACHED_TOPS, set())
        self.assertEqual(windows.ATTACHED_PIDS, frozenset())
        self.assertEqual(windows.modal_probe(), [])

    def test_attach_replaces_the_tops_set_instead_of_mutating_it(self):
        self.patch(windows, "ie_mode_tabs", lambda enum_fn=None: [self.tab()])
        before = windows.ATTACHED_TOPS
        call("ie_attach", {"hwnd": 11})
        self.assertIsNot(windows.ATTACHED_TOPS, before)
        self.assertEqual(before, set())
        held = windows.ATTACHED_TOPS
        call("ie_detach")
        self.assertEqual(held, {10})

    def test_attach_refuses_a_window_ie_tabs_does_not_list(self):
        self.patch(windows, "ie_mode_tabs", lambda enum_fn=None: [self.tab()])
        result = call("ie_attach", {"hwnd": 99})
        self.assertTrue(result.get("isError"))
        self.assertIn("ie_tabs", text_of(result))
        self.assertIsNone(backends.DEFAULT_TARGET)
        self.assertEqual(windows.ATTACHED_TOPS, set())
        for bad in ({}, {"hwnd": "x"}, {"hwnd": True}, {"hwnd": 0}):
            self.assertTrue(call("ie_attach", bad).get("isError"), bad)
        self.assertIsNone(backends.DEFAULT_TARGET)

    def test_attach_to_a_second_tab_replaces_the_first(self):
        tabs = [self.tab(11, 10), self.tab(21, 20)]
        self.patch(windows, "ie_mode_tabs", lambda enum_fn=None: tabs)
        call("ie_attach", {"hwnd": 11})
        call("ie_attach", {"hwnd": 21})
        self.assertEqual(backends.DEFAULT_TARGET, "tab:21")
        self.assertEqual(backends.ATTACHED_CHAINS, {21: [20]})
        self.assertEqual(windows.ATTACHED_TOPS, {20})

    def test_attach_reports_a_tab_it_cannot_read_and_stays_detached(self):
        self.patch(windows, "ie_mode_tabs", lambda enum_fn=None: [self.tab()])
        def unreadable(target):
            raise RuntimeError("no COM")
        self.patch(attach, "_probe_tab", unreadable)
        result = call("ie_attach", {"hwnd": 11})
        self.assertTrue(result.get("isError"), result)
        self.assertIn("no COM", text_of(result))
        self.assertIsNone(backends.DEFAULT_TARGET)
        self.assertEqual(windows.ATTACHED_TOPS, set())
        self.assertEqual(backends.ATTACHED_CHAINS, {})

    def test_ie_tabs_lists_tabs_and_marks_the_attached_one(self):
        self.patch(windows, "ie_mode_tabs", lambda enum_fn=None: [self.tab(), self.tab(21, 20)])
        call("ie_attach", {"hwnd": 11})
        rows = json.loads(text_of(call("ie_tabs")))
        self.assertEqual([(r["hwnd"], r["attached"]) for r in rows], [(11, True), (21, False)])
        self.assertEqual(rows[0]["target"], "tab:11")

    def test_ie_tabs_detaches_when_the_attached_tab_is_gone(self):
        self.patch(windows, "ie_mode_tabs", lambda enum_fn=None: [self.tab()])
        call("ie_attach", {"hwnd": 11})
        self.patch(messages, "is_window", lambda hwnd: hwnd != 11)
        call("ie_tabs")
        self.assertIsNone(backends.DEFAULT_TARGET)
        self.assertEqual(windows.ATTACHED_TOPS, set())

    def test_attach_instruments_the_tab_and_says_so(self):
        drained = []
        self.patch(consoletools, "drain_target", lambda target: drained.append(target) or 0)
        self.patch(windows, "ie_mode_tabs", lambda enum_fn=None: [self.tab()])
        result = json.loads(text_of(call("ie_attach", {"hwnd": 11})))
        self.assertEqual(drained, ["tab:11"])
        self.assertTrue(result["instrumented"])

    def test_attach_does_not_claim_instrumentation_that_did_not_happen(self):
        self.patch(windows, "ie_mode_tabs", lambda enum_fn=None: [self.tab()])
        self.patch(consoletools, "drain_target", lambda target: None)
        self.assertFalse(json.loads(text_of(call("ie_attach", {"hwnd": 11})))["instrumented"])
        call("ie_detach")
        calls = []
        self.patch(consoletools, "drain_target", lambda target: calls.append(target) or 0)
        with mock.patch.object(config, "SETTINGS", dataclasses.replace(
                config.SETTINGS, instrument=False)):
            result = json.loads(text_of(call("ie_attach", {"hwnd": 11})))
        self.assertFalse(result["instrumented"])
        self.assertEqual(calls, [])

    def test_the_drain_hook_drains_the_attached_tab_after_every_call(self):
        drained = []
        self.patch(consoletools, "drain_target", lambda target: drained.append(target) or 0)
        self.patch(windows, "_enum_top_level", lambda: [])
        self.patch(windows, "ie_mode_tabs", lambda enum_fn=None: [self.tab()])
        protocol.AFTER_CALL = [consoletools.dialog_drain]
        call("ie_attach", {"hwnd": 11})
        del drained[:]
        call("ie_tabs")
        self.assertEqual(drained, ["tab:11"])

    def test_ie_attach_is_refused_while_another_session_is_starting(self):
        self.patch(windows, "ie_mode_tabs", lambda enum_fn=None: [self.tab()])
        self.patch(windows, "other_sessions_starting", lambda: 1)
        result = call("ie_attach", {"hwnd": 11})
        self.assertTrue(result.get("isError"))
        self.assertIn("starting", text_of(result))
        self.assertIsNone(backends.DEFAULT_TARGET)
        listing = json.loads(text_of(call("ie_tabs")))
        self.assertIn("starting", listing["note"])
        self.assertEqual([t["hwnd"] for t in listing["tabs"]], [11])

    def test_a_failed_reattach_keeps_the_existing_attachment(self):
        tabs = [self.tab(11, 10), self.tab(21, 20)]
        self.patch(windows, "ie_mode_tabs", lambda enum_fn=None: tabs)
        call("ie_attach", {"hwnd": 11})

        def only_the_first(target):
            if target == "tab:21":
                raise RuntimeError("no COM")
            return "u"
        self.patch(attach, "_probe_tab", only_the_first)
        self.assertTrue(call("ie_attach", {"hwnd": 21}).get("isError"))
        self.assertEqual(backends.DEFAULT_TARGET, "tab:11")
        self.assertEqual(windows.ATTACHED_TOPS, {10})
        self.assertEqual(backends.ATTACHED_CHAINS, {11: [10]})

    def test_default_target_is_never_none_during_a_reattach(self):
        tabs = [self.tab(11, 10), self.tab(21, 20)]
        self.patch(windows, "ie_mode_tabs", lambda enum_fn=None: tabs)
        call("ie_attach", {"hwnd": 11})
        seen, stop = [], threading.Event()

        def sample():
            while not stop.wait(0.0005):  # waiting gives the attaching thread the GIL
                seen.append(backends.DEFAULT_TARGET)
        sampler = threading.Thread(target=sample, daemon=True)
        sampler.start()
        for hwnd in (21, 11) * 10:
            call("ie_attach", {"hwnd": hwnd})
        stop.set()
        sampler.join()
        self.assertNotIn(None, seen)

    def test_prune_drops_a_dead_top_when_the_modal_probe_input_is_read(self):
        self.patch(windows, "ie_mode_tabs", lambda enum_fn=None: [self.tab()])
        call("ie_attach", {"hwnd": 11})
        self.patch(messages, "is_window", lambda hwnd: hwnd != 10)  # the top is gone, the tab is not
        protocol._probe()
        self.assertIsNone(backends.DEFAULT_TARGET)
        self.assertEqual(windows.ATTACHED_TOPS, set())

    def test_prune_drops_a_dead_tab_before_the_driver_refusal(self):
        self.patch(windows, "ie_mode_tabs", lambda enum_fn=None: [self.tab()])
        call("ie_attach", {"hwnd": 11})
        self.patch(messages, "is_window", lambda hwnd: hwnd != 11)
        self.ensure.side_effect = None
        self.ensure.return_value = mock.Mock(title="t", current_url="u")
        result = call("ie_wait_ready", {"timeout": 1})
        self.assertNotIn("attached to tab", text_of(result))
        self.assertIsNone(backends.DEFAULT_TARGET)

    def test_a_hung_com_probe_in_ie_attach_never_blocks_the_protocol(self):
        # run_tool's refusal and the modal probe take ATTACH_LOCK outside any watchdog, so
        # ie_attach must not hold it while it talks to the (possibly hung) page.
        backends.DEFAULT_TARGET = "tab:5"
        self.patch(windows, "ie_mode_tabs", lambda enum_fn=None: [self.tab()])
        entered, release = threading.Event(), threading.Event()

        def hung(target):
            entered.set()
            release.wait(10)
            return "u"
        self.patch(attach, "_probe_tab", hung)
        attaching = threading.Thread(target=lambda: call("ie_attach", {"hwnd": 11}), daemon=True)
        attaching.start()
        self.addCleanup(attaching.join, 5)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(5))
        results = []
        other = threading.Thread(target=lambda: (protocol._probe(), results.append(
            call("ie_wait_ready", {}))), daemon=True)
        other.start()
        other.join(2)
        self.assertFalse(other.is_alive(), "the protocol waited on the attach lock")
        self.assertIn("ie_detach", text_of(results[0]))
        release.set()
        attaching.join(5)
        self.assertEqual(backends.DEFAULT_TARGET, "tab:11")

    def test_attach_fails_when_the_tab_closed_during_the_probe(self):
        self.patch(windows, "ie_mode_tabs", lambda enum_fn=None: [self.tab()])
        self.patch(attach, "_probe_tab", lambda target: self.patch(
            messages, "is_window", lambda hwnd: hwnd != 11) or "u")
        result = call("ie_attach", {"hwnd": 11})
        self.assertTrue(result.get("isError"))
        self.assertIn("closed", text_of(result))
        self.assertIsNone(backends.DEFAULT_TARGET)

    def test_ie_tabs_waits_for_the_attach_lock(self):
        self.patch(windows, "ie_mode_tabs", lambda enum_fn=None: [])
        done = threading.Event()
        with backends.ATTACH_LOCK:
            thread = threading.Thread(target=lambda: (call("ie_tabs"), done.set()), daemon=True)
            thread.start()
            self.assertFalse(done.wait(0.5))  # prune + read of the number is one locked step
        self.assertTrue(done.wait(5))

    def test_attach_description_says_ie_close_needs_a_detach(self):
        self.assertIn("ie_close needs ie_detach", protocol.REGISTRY["ie_attach"].description)

    def test_tools_work_without_a_driver_lock(self):
        self.patch(windows, "ie_mode_tabs", lambda enum_fn=None: [self.tab()])
        with protocol.driver_hold("other", 1):
            self.assertFalse(call("ie_tabs").get("isError"))
            self.assertFalse(call("ie_attach", {"hwnd": 11}).get("isError"))
            self.assertFalse(call("ie_detach").get("isError"))

    def test_driver_tools_refused_while_attached(self):
        backends.DEFAULT_TARGET = "tab:5"
        for name in ("ie_wait_ready", "ie_open"):
            args = {"url": "http://127.0.0.1/"} if name == "ie_open" else {}
            result = call(name, args)
            self.assertTrue(result.get("isError"), name)
            self.assertIn("ie_detach", text_of(result))
            self.assertIn("attached to tab 5", text_of(result))
        self.ensure.assert_not_called()

    def test_dialog_without_target_refused_while_attached(self):
        backends.DEFAULT_TARGET = "tab:5"
        result = call("ie_dialog", {})
        self.assertTrue(result.get("isError"))
        self.assertIn("ie_detach", text_of(result))
        explicit = call("ie_dialog", {"target": "tab:5"})
        self.assertTrue(explicit.get("isError"))
        self.ensure.assert_not_called()

    def test_driver_tools_still_run_when_not_attached(self):
        backends.DEFAULT_TARGET = None
        fake = mock.Mock()
        fake.title, fake.current_url = "t", "u"
        self.ensure.side_effect = None
        self.ensure.return_value = fake
        result = call("ie_wait_ready", {"timeout": 1})
        self.assertNotIn("ie_detach", text_of(result))
        self.ensure.assert_called()

    def test_resolve_tab_target_uses_com_backend(self):
        backends.DEFAULT_TARGET = "tab:5"
        backend = backends.resolve_backend(None)
        self.assertIsInstance(backend, backends.ComBackend)
        self.assertEqual((backend.kind, backend.hwnd), ("tab", 5))


class AttachedHelperTests(AttachTestCase):
    """Seen live: the helper that owns a tab's alerts is an ownerless window of the tab's IE
    process whose caption equals the browser window's, not a window owned by the top."""

    def world(self, helper_title="Form - Edge", helper_pid=EDGE + 1):
        fake = [raw(10, pid=EDGE, title="Form - Edge", children=[RawChild(11, IE_SERVER, 0, True)]),
                raw(20, pid=EDGE, title="Other - Edge"),
                raw(50, pid=helper_pid, cls=windows.HELPER_CLASS, title=helper_title),
                alert_window(60, owner=50)]
        fake[-1].pid = helper_pid
        return fake

    def probe(self, fake):
        self.patch(windows, "_enum_top_level", lambda: list(fake))
        return [w["hwnd"] for w in windows.modal_probe()]

    def test_an_alert_of_the_attached_tabs_helper_is_seen(self):
        windows.ATTACHED_TOPS, windows.ATTACHED_PIDS = {10}, frozenset({EDGE + 1})
        self.assertEqual(self.probe(self.world()), [60])

    def test_a_helper_with_another_caption_or_process_is_not_the_tabs(self):
        windows.ATTACHED_TOPS, windows.ATTACHED_PIDS = {10}, frozenset({EDGE + 1})
        self.assertEqual(self.probe(self.world(helper_title="Other - Edge")), [])
        self.assertEqual(self.probe(self.world(helper_pid=EDGE + 2)), [])

    def test_two_windows_with_the_same_caption_make_the_helper_ambiguous(self):
        windows.ATTACHED_TOPS, windows.ATTACHED_PIDS = {10}, frozenset({EDGE + 1})
        fake = self.world()
        fake.append(raw(25, pid=EDGE, title="Form - Edge"))  # another window, same caption
        self.assertEqual(self.probe(fake), [])
        # an invisible same-titled window does not count
        fake[-1].visible = False
        self.assertEqual(self.probe(fake), [60])

    def test_an_empty_caption_never_attributes_a_helper(self):
        windows.ATTACHED_TOPS, windows.ATTACHED_PIDS = {10}, frozenset({EDGE + 1})
        fake = self.world(helper_title="")
        fake[0].title = ""
        self.assertEqual(self.probe(fake), [])

    def test_nothing_is_seen_when_no_tab_is_attached(self):
        self.assertEqual(self.probe(self.world()), [])


class AttachedDialogOwnerTests(AttachTestCase):
    def window(self, owner_pid):
        return WindowInfo(hwnd=30, pid=owner_pid, cls="#32770", title="", kind="script_dialog",
                          owner_hwnd=10, visible=True, owner_cls=windows.HELPER_CLASS,
                          owner_pid=owner_pid)

    def test_a_helper_of_the_attached_tabs_process_owns_dialogs(self):
        windows.ATTACHED_PIDS = frozenset({777})
        self.assertTrue(scriptdialogs._owned_by_a_dialog_host(self.window(777)))

    def test_a_helper_of_any_other_process_does_not(self):
        windows.ATTACHED_PIDS = frozenset({777})
        self.assertFalse(scriptdialogs._owned_by_a_dialog_host(self.window(888)))
        windows.ATTACHED_PIDS = frozenset()
        self.assertFalse(scriptdialogs._owned_by_a_dialog_host(self.window(777)))


class FidelityTests(AttachTestCase):
    def test_the_leftover_instrumentation_row_appears_once_a_tab_was_attached(self):
        from ie_mcp.tools import diagnostics
        row = "Instrumentation left in an attached tab"
        self.assertFalse(any(row in r for r in diagnostics.modifications()))
        self.patch(windows, "ie_mode_tabs", lambda enum_fn=None: [self.tab()])
        call("ie_attach", {"hwnd": 11})
        call("ie_detach")  # the instrumentation stays in the tab: still listed
        self.assertTrue(any(row in r for r in diagnostics.modifications()))
        with mock.patch.object(config, "SETTINGS", dataclasses.replace(
                config.SETTINGS, instrument=False)):
            self.assertFalse(any(row in r for r in diagnostics.modifications()))


if __name__ == "__main__":
    unittest.main()
