"""Unit tests for the Win32 layer (processes, windows, HTML documents). Fakes only: no real
windows, no real COM, and none of them needs pywin32 to be installed."""
import io
import os
import sys
import types
import unittest
from unittest import mock

from ie_mcp import protocol
from ie_mcp.session import SESSION, IeSession
from ie_mcp.win32 import htmldoc, processes, windows
from ie_mcp.win32.windows import RawChild, RawWindow

IE_SERVER = "Internet Explorer_Server"
DLG_FRAME = "Internet Explorer_TridentDlgFrame"
HELPER = "Alternate Modal Top Most"
EDGE_PID = 100
OTHER_PID = 9


def ie_child(hwnd=1000):
    return RawChild(hwnd=hwnd, cls=IE_SERVER, ctrl_id=0, visible=True)


def top(hwnd, cls="Chrome_WidgetWin_1", pid=EDGE_PID, owner=None, title="", visible=True,
        children=()):
    return RawWindow(hwnd=hwnd, pid=pid, cls=cls, title=title, owner=owner, visible=visible,
                     children=children if callable(children) else list(children))


def alert(hwnd, owner, pid=EDGE_PID, visible=True):
    # Q8: Button 2, Static 20, Static 65535.
    return top(hwnd, "#32770", pid, owner, "Message from webpage", visible, [
        RawChild(hwnd + 1, "Button", 2, True), RawChild(hwnd + 2, "Static", 20, True),
        RawChild(hwnd + 3, "Static", 65535, True)])


def prompt(hwnd, owner, pid=EDGE_PID, visible=True):
    # Q8/D14: an Edit child with control ID 8133.
    return top(hwnd, "#32770", pid, owner, "host needs some information", visible, [
        RawChild(hwnd + 1, "Button", 1, True), RawChild(hwnd + 2, "Edit", 8133, True)])


def enumerate_with(raw, pids=frozenset({EDGE_PID}), **kwargs):
    with mock.patch.object(SESSION, "main_hwnd", kwargs.pop("main_hwnd", None), create=True):
        return windows.enumerate_windows(set(pids), enum_fn=lambda: raw, **kwargs)


def by_hwnd(infos):
    return {w.hwnd: w for w in infos}


class ProcessTreeTests(unittest.TestCase):
    def test_process_tree_walks_children(self):
        rows = [processes.ProcessRow(1, 0, "a.exe"), processes.ProcessRow(2, 1, "b.exe"),
                processes.ProcessRow(3, 2, "c.exe"), processes.ProcessRow(4, 99, "d.exe")]
        self.assertEqual(processes.process_tree(1, rows), {1, 2, 3})

    def test_process_tree_survives_a_parent_cycle(self):
        rows = [processes.ProcessRow(1, 2, "a.exe"), processes.ProcessRow(2, 1, "b.exe")]
        self.assertEqual(processes.process_tree(1, rows), {1, 2})

    def test_snapshot_is_cached_for_two_seconds(self):
        calls = []

        def read():
            calls.append(1)
            return [processes.ProcessRow(len(calls), 0, "x.exe")]

        clock = [100.0]
        with mock.patch.object(processes, "_read_snapshot", side_effect=read), \
                mock.patch.object(processes.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(processes, "_cache", None):
            first = processes.snapshot()
            clock[0] += 1.9
            self.assertEqual(processes.snapshot(), first)
            self.assertEqual(len(calls), 1)
            clock[0] += 0.2
            self.assertNotEqual(processes.snapshot(), first)
            self.assertEqual(len(calls), 2)
            processes.snapshot(fresh=True)  # a "before/after" pair must never see old data
            self.assertEqual(len(calls), 3)


    def test_an_empty_snapshot_is_not_cached(self):
        reads = [[], [processes.ProcessRow(1, 0, "x.exe")]]
        with mock.patch.object(processes, "_read_snapshot", side_effect=lambda: reads.pop(0)),                 mock.patch.object(processes, "_log") as log,                 mock.patch.object(processes, "_cache", None):
            self.assertEqual(processes.snapshot(), [])
            log.assert_called_once()
            # The very next call reads again instead of replaying the failure.
            self.assertEqual(processes.snapshot(), [processes.ProcessRow(1, 0, "x.exe")])


class RealWin32SmokeTests(unittest.TestCase):
    """The ctypes layer against the real desktop. Read-only: no window is created or messaged."""

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_snapshot_lists_this_process_with_its_real_parent(self):
        rows = processes.snapshot(fresh=True)
        mine = [r for r in rows if r.pid == os.getpid()]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0].ppid, os.getppid())
        self.assertTrue(mine[0].exe.lower().startswith("python"))

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_enumerate_windows_runs_on_the_real_desktop(self):
        self.assertIsInstance(windows.enumerate_windows({os.getpid()}), list)
        everything = windows.enumerate_windows({r.pid for r in processes.snapshot(fresh=True)})
        self.assertTrue(everything)
        self.assertTrue(all(isinstance(w.hwnd, int) and w.hwnd > 0 for w in everything))


class ClassifyTests(unittest.TestCase):
    def test_classify_trident_dialog_frame_as_html_dialog(self):
        self.assertEqual(windows.classify(DLG_FRAME, True, False, 5, None), "html_dialog")

    def test_classify_owned_alert_class_as_script_dialog(self):
        self.assertEqual(windows.classify("#32770", False, True, 5, None), "script_dialog")

    def test_classify_alert_owned_by_html_dialog(self):
        # An alert raised inside a showModalDialog window is owned by its TridentDlgFrame; the
        # flag is computed by enumerate_windows, which counts a session html_dialog as owner.
        raw = [top(7, DLG_FRAME, owner=6, children=[ie_child()]), top(6, HELPER),
               alert(20, owner=7)]
        got = by_hwnd(enumerate_with(raw))
        self.assertEqual(got[7].kind, "html_dialog")
        self.assertEqual(got[20].kind, "script_dialog")

    def test_classify_unowned_32770_as_system_dialog(self):
        self.assertEqual(windows.classify("#32770", False, False, 5, None), "system_dialog")

    def test_classify_prompt_needs_a_session_owner(self):
        self.assertEqual(windows.classify("#32770", False, True, 5, None, is_prompt=True),
                         "script_dialog")
        self.assertEqual(windows.classify("#32770", False, False, 5, None, is_prompt=True),
                         "system_dialog")

    def test_main_vs_popup_by_main_hwnd(self):
        self.assertEqual(windows.classify("Chrome_WidgetWin_1", True, False, 5, 5), "main")
        self.assertEqual(windows.classify("Chrome_WidgetWin_1", True, False, 6, 5), "popup")
        self.assertEqual(windows.classify("Chrome_WidgetWin_1", True, False, 6, None), "popup")

    def test_other_windows_are_other(self):
        self.assertEqual(windows.classify("Chrome_WidgetWin_1", False, False, 5, None), "other")
        self.assertEqual(windows.classify(HELPER, False, False, 5, None), "other")


class PickMainHwndTests(unittest.TestCase):
    def test_main_hwnd_is_the_new_ie_window(self):
        self.assertEqual(windows.pick_main_hwnd({10}, {10: "old", 20: "new"}, lambda: "x"), 20)

    def test_several_new_windows_pick_the_one_titled_like_the_driver(self):
        after = {10: "old", 20: "Other - Edge", 30: "Fixture page - Microsoft Edge"}
        self.assertEqual(windows.pick_main_hwnd({10}, after, lambda: "Fixture page"), 30)

    def test_no_new_window_is_none(self):
        self.assertIsNone(windows.pick_main_hwnd({10}, {10: "old"}, lambda: "x"))

    def test_unknown_before_snapshot_is_none(self):
        self.assertIsNone(windows.pick_main_hwnd(None, {20: "new"}, lambda: "x"))

    def test_several_new_and_no_title_match_is_none(self):
        self.assertIsNone(windows.pick_main_hwnd(set(), {1: "a", 2: "b"}, lambda: "zzz"))

    def test_a_failing_title_read_is_none(self):
        def boom():
            raise RuntimeError("driver gone")

        self.assertIsNone(windows.pick_main_hwnd(set(), {1: "a", 2: "b"}, boom))


class EnumerateTests(unittest.TestCase):
    def test_enumerate_marks_prompt_as_script_dialog(self):
        raw = [top(1, children=[ie_child()]), prompt(10, owner=1),
               top(20, DLG_FRAME, children=[ie_child(21)]),
               prompt(30, owner=None)]
        got = by_hwnd(enumerate_with(raw))
        self.assertEqual(got[10].kind, "script_dialog")
        self.assertTrue(got[10].is_prompt)
        self.assertEqual(got[20].kind, "html_dialog")
        self.assertFalse(got[20].is_prompt)
        # D14's rule matches this unowned window, but nothing in the session owns it.
        self.assertTrue(got[30].is_prompt)
        self.assertEqual(got[30].kind, "system_dialog")

    def test_alert_owned_by_the_helper_is_a_script_dialog(self):
        # D6: the helper has no owner of its own, but it is a session window by its PID.
        raw = [top(6, HELPER), alert(20, owner=6)]
        self.assertEqual(by_hwnd(enumerate_with(raw))[20].kind, "script_dialog")

    def test_a_helper_of_another_process_does_not_make_an_alert_ours(self):
        raw = [top(6, HELPER, pid=OTHER_PID), alert(20, owner=6, pid=OTHER_PID)]
        self.assertEqual(enumerate_with(raw), [])
        raw = [top(6, HELPER, pid=OTHER_PID), alert(20, owner=6)]
        self.assertEqual(by_hwnd(enumerate_with(raw))[20].kind, "system_dialog")

    def test_enumerate_filters_to_session_pids(self):
        raw = [top(1, pid=1, children=[ie_child()]), top(2, pid=2, children=[ie_child(1001)]),
               top(3, pid=OTHER_PID, children=[ie_child(1002)])]
        got = enumerate_with(raw, pids={1, 2})
        self.assertEqual([w.hwnd for w in got], [1, 2])

    def test_main_and_popup(self):
        raw = [top(1, children=[ie_child()]), top(2, children=[ie_child(1001)])]
        got = by_hwnd(enumerate_with(raw, main_hwnd=2))
        self.assertEqual((got[1].kind, got[2].kind), ("popup", "main"))

    def test_fields_are_filled_in(self):
        raw = [top(1, title="T", visible=False, children=[ie_child()]), alert(20, owner=1)]
        got = by_hwnd(enumerate_with(raw))
        self.assertEqual((got[1].pid, got[1].cls, got[1].title, got[1].visible, got[1].url),
                         (EDGE_PID, "Chrome_WidgetWin_1", "T", False, ""))
        self.assertEqual(got[20].owner_hwnd, 1)
        self.assertIsNone(got[1].owner_hwnd)

    def test_owner_cycle_terminates(self):
        raw = [top(1, owner=2), top(2, owner=1)]
        self.assertEqual([w.kind for w in enumerate_with(raw)], ["other", "other"])

    def test_enumerate_classifies_alert_owned_through_attached_chain(self):
        raw = [alert(60, owner=50, pid=OTHER_PID),
               top(70, owner=50, pid=OTHER_PID),
               alert(80, owner=70, pid=OTHER_PID),
               top(90, pid=OTHER_PID)]  # unrelated window of the same process: not listed
        got = by_hwnd(enumerate_with(raw, pids=set(), attached_tops={50}))
        self.assertEqual(sorted(got), [60, 70, 80])
        self.assertEqual(got[60].kind, "script_dialog")
        self.assertEqual(got[80].kind, "script_dialog")

    def test_enumerate_includes_attached_top_dialogs(self):
        raw = [alert(60, owner=50, pid=OTHER_PID)]
        got = enumerate_with(raw, pids={EDGE_PID}, attached_tops={50})
        self.assertEqual([w.hwnd for w in got], [60])

    def test_children_are_read_lazily_and_once(self):
        reads = []

        def children():
            reads.append(1)
            return [ie_child()]

        raw = [top(1, children=children), top(2, pid=OTHER_PID, children=children)]
        got = enumerate_with(raw)
        self.assertEqual([w.kind for w in got], ["popup"])
        self.assertEqual(len(reads), 1)  # the foreign window's children were never read

    def test_with_url_reads_documents_and_a_failure_leaves_empty(self):
        raw = [top(1, children=[ie_child(11)]), top(2, children=[ie_child(12)]),
               alert(20, owner=1)]

        def url_of(hwnd):
            if hwnd == 12:
                raise OSError("no document")
            return f"http://127.0.0.1/{hwnd}"

        with mock.patch.object(htmldoc, "document_url", side_effect=url_of),                 mock.patch("ie_mcp.session.log"):
            got = by_hwnd(enumerate_with(raw, with_url=True))
        self.assertEqual(got[1].url, "http://127.0.0.1/11")
        self.assertEqual(got[2].url, "")
        self.assertEqual(got[20].url, "")  # a script dialog has no document

    test_failed_url_read_leaves_empty = test_with_url_reads_documents_and_a_failure_leaves_empty

    def test_a_failed_url_read_is_logged_and_missing_pywin32_only_once(self):
        raw = [top(1, children=[ie_child(11)]), top(2, children=[ie_child(12)])]
        missing = RuntimeError(htmldoc._NO_PYWIN32)
        with mock.patch.object(htmldoc, "document_url", side_effect=missing),                 mock.patch.object(windows, "_warned", set()),                 mock.patch("ie_mcp.session.log") as log:
            got = enumerate_with(raw, with_url=True)
        self.assertEqual([w.url for w in got], ["", ""])
        self.assertEqual(log.call_count, 1)
        self.assertIn("pywin32", log.call_args[0][0])
        with mock.patch.object(htmldoc, "document_url", side_effect=OSError("gone")),                 mock.patch("ie_mcp.session.log") as log:
            enumerate_with(raw, with_url=True)
        self.assertEqual(log.call_count, 2)  # other failures are logged for every window


class ModalProbeTests(unittest.TestCase):
    def setUp(self):
        for patcher in (mock.patch.object(SESSION, "edge_pid", None),
                        mock.patch.object(SESSION, "main_hwnd", None, create=True),
                        mock.patch.object(windows, "ATTACHED_TOPS", set())):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_modal_probe_empty_without_session(self):
        with mock.patch.object(windows, "_enum_top_level") as enum:
            self.assertEqual(windows.modal_probe(), [])
        enum.assert_not_called()

    def test_session_window_pids_follow_the_edge_process_tree(self):
        self.assertEqual(windows.session_window_pids(), set())
        rows = [processes.ProcessRow(100, 1, "msedge.exe"), processes.ProcessRow(101, 100, "msedge.exe"),
                processes.ProcessRow(5, 1, "other.exe")]
        with mock.patch.object(SESSION, "edge_pid", 100), \
                mock.patch.object(processes, "snapshot", return_value=rows):
            self.assertEqual(windows.session_window_pids(), {100, 101})

    def test_probe_lists_visible_modal_windows_as_dicts(self):
        raw = [top(1, children=[ie_child()]), alert(20, owner=1),
               alert(30, owner=1, visible=False),       # Edge keeps hidden #32770 windows
               top(40, DLG_FRAME, children=[ie_child(41)], title="Lookup"),
               top(50, "#32770", owner=None, title="Save as"),
               prompt(60, owner=1)]
        with mock.patch.object(SESSION, "edge_pid", EDGE_PID), \
                mock.patch.object(windows, "session_window_pids", return_value={EDGE_PID}), \
                mock.patch.object(windows, "_enum_top_level", return_value=raw):
            got = {d["hwnd"]: d for d in windows.modal_probe()}
        self.assertEqual(sorted(got), [20, 40, 50, 60])
        self.assertEqual([got[h]["kind"] for h in (20, 40, 50, 60)],
                         ["script_dialog", "html_dialog", "system_dialog", "script_dialog"])
        for d in got.values():
            for key in ("hwnd", "kind", "title", "pid", "cls", "owner_hwnd", "is_prompt"):
                self.assertIn(key, d)
        self.assertEqual(got[40]["title"], "Lookup")
        self.assertTrue(got[60]["is_prompt"])

    def test_probe_includes_dialogs_owned_by_an_attached_tab(self):
        raw = [alert(60, owner=50, pid=OTHER_PID)]
        with mock.patch.object(windows, "ATTACHED_TOPS", {50}), \
                mock.patch.object(windows, "_enum_top_level", return_value=raw):
            self.assertEqual([d["hwnd"] for d in windows.modal_probe()], [60])

    def test_probe_never_reads_url(self):
        raw = [top(1, children=[ie_child()]), alert(20, owner=1)]
        com = mock.Mock()
        fake_pythoncom = types.SimpleNamespace(CoInitialize=com, ObjectFromLresult=com)
        with mock.patch.dict(sys.modules, {"pythoncom": fake_pythoncom}), \
                mock.patch.object(htmldoc, "document_from_hwnd", com), \
                mock.patch.object(htmldoc, "document_url", com), \
                mock.patch.object(windows, "session_window_pids", return_value={EDGE_PID}), \
                mock.patch.object(windows, "_enum_top_level", return_value=raw):
            probe = windows.modal_probe()
        self.assertTrue(probe)
        com.assert_not_called()
        self.assertTrue(all(d.get("url", "") == "" for d in probe))

    def test_protocol_installs_the_real_probe(self):
        self.assertIs(protocol.MODAL_PROBE, windows.modal_probe)


def fake_ie_server_windows(classes, visible=()):
    """Patches for the two thin Win32 wrappers ie_server_child is built from."""
    return (mock.patch.object(htmldoc, "window_class", side_effect=lambda h: classes.get(h, "")),
            mock.patch.object(htmldoc, "_is_visible", side_effect=lambda h: h in visible))


class HtmlDocTests(unittest.TestCase):
    def test_ie_server_child_returns_itself(self):
        with mock.patch.object(htmldoc, "window_class", return_value=IE_SERVER):
            self.assertEqual(htmldoc.ie_server_child(5), 5)

    def test_ie_server_child_prefers_a_visible_descendant(self):
        patches = fake_ie_server_windows({1: "TabWindowClass"}, visible={8})
        with patches[0], patches[1], \
                mock.patch.object(htmldoc, "ie_server_descendants", return_value=[7, 8]):
            self.assertEqual(htmldoc.ie_server_child(1), 8)

    def test_ie_server_child_none_when_only_hidden_or_absent(self):
        patches = fake_ie_server_windows({1: "TabWindowClass"}, visible=())
        with patches[0], patches[1]:
            with mock.patch.object(htmldoc, "ie_server_descendants", return_value=[7]):
                self.assertIsNone(htmldoc.ie_server_child(1))
            with mock.patch.object(htmldoc, "ie_server_descendants", return_value=[]):
                self.assertIsNone(htmldoc.ie_server_child(1))

    def test_document_from_hwnd_without_pywin32_names_extra(self):
        # A None entry in sys.modules makes `import pythoncom` raise ImportError.
        with mock.patch.dict(sys.modules, {"pythoncom": None, "win32gui": None}):
            with self.assertRaises(RuntimeError) as caught:
                htmldoc.document_from_hwnd(5)
        self.assertIn("ie-mcp[win32]", str(caught.exception))

    def test_document_from_hwnd_asks_the_server_window(self):
        calls = {}
        fake_pythoncom = types.SimpleNamespace(
            IID_IDispatch="iid", ObjectFromLresult=lambda lres, iid, wp: ("doc", lres, iid, wp))

        def send(hwnd, msg, wparam, lparam, flags, timeout):
            calls["send"] = (hwnd, msg, flags, timeout)
            return 1, 777

        fake_gui = types.SimpleNamespace(RegisterWindowMessage=lambda name: 0xC000 + len(name),
                                         SendMessageTimeout=send)
        fake_con = types.SimpleNamespace(SMTO_ABORTIFHUNG=2)
        with mock.patch.dict(sys.modules, {"pythoncom": fake_pythoncom, "win32gui": fake_gui,
                                           "win32con": fake_con}):
            doc = htmldoc.document_from_hwnd(5, timeout_ms=250)
        self.assertEqual(doc, ("doc", 777, "iid", 0))
        self.assertEqual(calls["send"], (5, 0xC000 + len("WM_HTML_GETOBJECT"), 2, 250))

    def test_a_failing_send_message_names_the_window(self):
        def send(*args):
            raise OSError("timed out")  # stands in for pywintypes.error

        fake_gui = types.SimpleNamespace(RegisterWindowMessage=lambda name: 1,
                                         SendMessageTimeout=send)
        with mock.patch.dict(sys.modules, {"pythoncom": types.SimpleNamespace(),
                                           "win32gui": fake_gui,
                                           "win32con": types.SimpleNamespace(SMTO_ABORTIFHUNG=2)}):
            with self.assertRaises(RuntimeError) as caught:
                htmldoc.document_from_hwnd(77)
        self.assertIn("window 77", str(caught.exception))

    def test_document_from_hwnd_zero_lresult_is_an_error(self):
        fake_pythoncom = types.SimpleNamespace(IID_IDispatch="iid", ObjectFromLresult=None)
        fake_gui = types.SimpleNamespace(RegisterWindowMessage=lambda name: 1,
                                         SendMessageTimeout=lambda *a: (1, 0))
        fake_con = types.SimpleNamespace(SMTO_ABORTIFHUNG=2)
        with mock.patch.dict(sys.modules, {"pythoncom": fake_pythoncom, "win32gui": fake_gui,
                                           "win32con": fake_con}):
            with self.assertRaises(RuntimeError):
                htmldoc.document_from_hwnd(5)


class FakeComError(Exception):
    def __init__(self, hresult):
        super().__init__(hresult, "fake", None, None)
        self.hresult = hresult


class ComCallTests(unittest.TestCase):
    def setUp(self):
        self.now = [0.0]
        fake_time = types.SimpleNamespace(
            monotonic=lambda: self.now[0],
            sleep=lambda s: self.now.__setitem__(0, self.now[0] + s))
        patcher = mock.patch.object(htmldoc, "time", fake_time)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_com_call_retries_rejected_calls(self):
        # Quirk: IE answers RPC_E_CALL_REJECTED / RPC_E_SERVERCALL_RETRYLATER while it is busy.
        outcomes = [FakeComError(-2147418111), FakeComError(0x8001010A), "ok"]  # signed + unsigned

        def fn(a, b):
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return (outcome, a, b)

        self.assertEqual(htmldoc.com_call(fn, 1, 2), ("ok", 1, 2))
        self.assertAlmostEqual(self.now[0], 0.2)

    def test_com_call_gives_up_after_one_second(self):
        def fn():
            raise FakeComError(0x80010001)

        with self.assertRaises(FakeComError):
            htmldoc.com_call(fn)
        # Ten 100 ms sleeps add up to a hair under 1 s in floating point, so one more may run.
        self.assertGreaterEqual(self.now[0], 0.99)
        self.assertLessEqual(self.now[0], 1.11)

    def test_com_call_does_not_retry_other_errors(self):
        attempts = []

        def fn():
            attempts.append(1)
            raise FakeComError(0x80004005)

        with self.assertRaises(FakeComError):
            htmldoc.com_call(fn)
        self.assertEqual(len(attempts), 1)

    def test_com_call_does_not_retry_plain_exceptions(self):
        attempts = []

        def fn():
            attempts.append(1)
            raise ValueError("no hresult")

        with self.assertRaises(ValueError):
            htmldoc.com_call(fn)
        self.assertEqual(len(attempts), 1)


class ComApartmentTests(unittest.TestCase):
    def test_com_apartment_initialises_and_uninitialises(self):
        events = []
        fake = types.SimpleNamespace(CoInitialize=lambda: events.append("init"),
                                     CoUninitialize=lambda: events.append("uninit"))
        with mock.patch.dict(sys.modules, {"pythoncom": fake}):
            with htmldoc.com_apartment():
                events.append("body")
        self.assertEqual(events, ["init", "body", "uninit"])

    def test_com_apartment_registered_in_call_contexts(self):
        fake = types.SimpleNamespace(CoInitialize=lambda: None, CoUninitialize=lambda: None)
        with mock.patch.object(protocol, "CALL_CONTEXTS", []) as contexts, \
                mock.patch.dict(sys.modules, {"pythoncom": fake}):
            self.assertTrue(protocol._register_com_context())
            self.assertTrue(protocol._register_com_context())  # idempotent
            self.assertEqual(contexts, [htmldoc.com_apartment])

    def test_com_apartment_not_registered_without_pywin32(self):
        with mock.patch.object(protocol, "CALL_CONTEXTS", []) as contexts, \
                mock.patch.dict(sys.modules, {"pythoncom": None}):
            self.assertFalse(protocol._register_com_context())
            self.assertEqual(contexts, [])


class SessionMainHwndTests(unittest.TestCase):
    def setUp(self):
        stderr = mock.patch.object(sys, "stderr", io.StringIO())  # the server log goes there
        stderr.start()
        self.addCleanup(stderr.stop)

    def test_main_hwnd_is_recorded_from_the_new_ie_window(self):
        s = IeSession()
        s.driver = types.SimpleNamespace(title="Fixture")
        s.edge_pid = 100
        with mock.patch.object(s, "_ie_tops", return_value={10: "old", 20: "Fixture - Edge"}) as tops:
            s._record_main_hwnd({10: "old"})
        self.assertEqual(s.main_hwnd, 20)
        tops.assert_called_once_with(100)  # only the session's own Edge tree is scanned

    def test_without_an_adopted_edge_the_main_window_stays_unknown(self):
        s = IeSession()
        s.edge_pid = None
        with mock.patch.object(s, "_ie_tops") as tops:
            s._record_main_hwnd({10: "old"})
        self.assertIsNone(s.main_hwnd)
        tops.assert_not_called()  # never scan every Edge window of the user's desktop

    def test_unknown_before_snapshot_leaves_it_unknown(self):
        s = IeSession()
        s.main_hwnd = 5
        with mock.patch.object(s, "_ie_tops") as tops:
            s._record_main_hwnd(None)
        self.assertIsNone(s.main_hwnd)
        tops.assert_not_called()

    def test_a_failing_window_scan_never_raises(self):
        s = IeSession()
        with mock.patch.object(processes, "snapshot", side_effect=OSError("boom")):
            self.assertIsNone(s._ie_tops())
        with mock.patch.object(s, "_ie_tops", side_effect=OSError("boom")):
            s._record_main_hwnd({})
        self.assertIsNone(s.main_hwnd)

    def test_teardown_forgets_the_main_window(self):
        s = IeSession()
        s.main_hwnd = 5
        with mock.patch.object(s, "kill_own_processes", return_value=[]):
            s.quit_browser()
            self.assertIsNone(s.main_hwnd)
            s.main_hwnd = 5
            s.force_kill()
            self.assertIsNone(s.main_hwnd)


if __name__ == "__main__":
    unittest.main()
