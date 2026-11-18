"""Unit tests for the page instrumentation and the console tools (plan Task 12).

Fakes only: no browser, no real windows, no COM. The JavaScript itself (instrument.js,
hook.js, drain.js) is checked for the Trident rules by ``test_js_resources`` and run for real
by ``tests/integration/it_console.py``.
"""
import dataclasses
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from ie_mcp import backends, config, js, protocol
from ie_mcp.capture.console import ConsoleRecord, ConsoleStore, parse_records
from ie_mcp.capture.redact import Redactor
from ie_mcp.capture.store import CaptureStore
from ie_mcp.session import SESSION
from ie_mcp.tools import console as consoletools
from ie_mcp.tools import dialogs
from ie_mcp.win32 import windows
from ie_mcp.win32.windows import RawChild
from tests.test_window_tools import FakeDriver as WindowDriver
from tests.test_window_tools import (TASKDIALOG_CHILDREN, WM_COMMAND, WindowToolsTestCase,
                                     alert_children, alert_info, call, first_text, info,
                                     is_error)

SPEC = protocol.REGISTRY["ie_click"]
INSTRUMENT_SOURCE = js.load("instrument.js")


def record(kind="console", level="log", text="hi", where="driver", age=0.0, id=0,
           dialog_id="", popup=False):
    return ConsoleRecord(id, kind, level, time.time() - age, text, where, dialog_id, popup)


def state(open_dialogs):
    """The pseudo-record drain.js ends with: the alert/confirm/prompt calls in flight."""
    return f"\x1fstate\x1f\x1f0\x1fopen={open_dialogs}"


class FakeCom:
    """Stands in for ``backends.ComBackend``: remembers how it was built and what it ran."""
    instances = []
    raw = ""
    fail = False

    def __init__(self, hwnd, kind="hwnd"):
        self.hwnd, self.kind = hwnd, kind
        self.owner = threading.get_ident()
        self.scripts = []
        FakeCom.instances.append(self)

    def js(self, script, frame, args):
        self.scripts.append((script, frame, list(args)))
        if FakeCom.fail:
            raise RuntimeError("COM call failed (the page is busy)")
        return FakeCom.raw


class ScriptDriver:
    """A driver that only runs ``execute_script``."""

    def __init__(self, raw=""):
        self.raw = raw
        self.calls = []

    def execute_script(self, script, *args):
        self.calls.append((script, args))
        return self.raw


class ConsoleTestCase(WindowToolsTestCase):
    """Window fakes of the dialog tests plus a fresh console store and a fake COM backend."""

    def setUp(self):
        super().setUp()
        self.store = ConsoleStore(Redactor())
        self.patch(consoletools, "STORE", self.store)
        self.patch(consoletools, "NETWORK_STORE", None)
        self.patch(consoletools, "INSTALLS", 0)
        self.patch(consoletools, "LAST_FLAGS", None)
        FakeCom.instances, FakeCom.raw, FakeCom.fail = [], "", False
        self.patch(backends, "ComBackend", FakeCom)
        self.patch(backends, "DEFAULT_TARGET", None)

    def patch(self, target, name, value):
        # create=True: SESSION.capture_active only exists once Task 13 wires the proxy.
        patcher = mock.patch.object(target, name, value, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def settings(self, **changes):
        self.patch(config, "SETTINGS", dataclasses.replace(config.SETTINGS, **changes))


class ParseRecordsTests(unittest.TestCase):
    def test_parse_records_splits_fields_and_unescapes(self):
        raw = "0\x1ferror\x1ferror\x1f1.5\x1fboom \\x1f x\x1e1/2\x1fconsole\x1flog\x1f2.5\x1fa\\\\b"
        first, second = parse_records(raw, "")
        self.assertEqual((first.where, first.kind, first.level, first.time),
                         ("0", "error", "error", 1.5))
        self.assertEqual(first.text, "boom \x1f x")
        self.assertEqual((second.where, second.text), ("1/2", "a\\b"))

    def test_where_is_the_target_then_the_frame_path(self):
        got = parse_records("\x1fopen\x1f\x1f1\x1fu\x1e3\x1fopen\x1f\x1f1\x1fv", "hwnd:7")
        self.assertEqual([r.where for r in got], ["hwnd:7", "hwnd:7/3"])

    def test_the_level_carries_the_dialog_id_and_the_popup_mark(self):
        raw = ("\x1fdialog-open\x1fconfirm#k3.2\x1f1\x1fSure?\x1e"
               "\x1fdialog\x1fconfirm#k3.2\x1f2\x1fSure? -> true\x1e"
               "\x1fconsole\x1flog@popup\x1f3\x1fhi\x1e"
               "\x1fdialog-open\x1falert#k4.1@popup\x1f4\x1fPop\x1e"
               "\x1fmodal\x1f\x1f5\x1fm")
        got = parse_records(raw, "driver")
        self.assertEqual([(r.kind, r.level, r.dialog_id, r.popup) for r in got], [
            ("dialog-open", "confirm", "k3.2", False), ("dialog", "confirm", "k3.2", False),
            ("console", "log", "", True), ("dialog-open", "alert", "k4.1", True),
            ("modal", "", "", False)])

    def test_malformed_records_are_dropped(self):
        got = parse_records("junk\x1e\x1e0\x1fonly\x1ffour\x1fx\x1e\x1fok\x1flv\x1fnotanumber\x1ft", "d")
        self.assertEqual([(r.kind, r.time) for r in got], [("ok", 0.0)])
        self.assertEqual(parse_records("", "d"), [])
        self.assertEqual(parse_records(None, "d"), [])


class StoreTests(unittest.TestCase):
    def test_store_since_id_and_kind_filter(self):
        store = ConsoleStore(Redactor())
        store.add_many([record("error", "error", "e1"), record("console", "log", "c1"),
                        record("dialog", "alert", "d1"), record("console", "warn", "c2")])
        self.assertEqual([r.id for r in store.list()], [1, 2, 3, 4])
        self.assertEqual([r.text for r in store.list(since_id=2)], ["d1", "c2"])
        self.assertEqual([r.text for r in store.list(kinds=["console"])], ["c1", "c2"])
        self.assertEqual([r.text for r in store.list(kinds=["console", "error"], since_id=1)],
                         ["c1", "c2"])
        self.assertEqual([r.id for r in store.list(limit=2)], [1, 2])
        store.clear()
        self.assertEqual(store.list(), [])
        store.add_many([record(text="after")])
        self.assertEqual([r.id for r in store.list()], [5])  # ids keep counting

    def test_the_store_is_a_ring_buffer(self):
        store = ConsoleStore(Redactor(), max_records=3)
        store.add_many([record(text=str(i)) for i in range(5)])
        self.assertEqual([r.text for r in store.list()], ["2", "3", "4"])

    def test_console_text_is_redacted(self):
        store = ConsoleStore(Redactor())
        store.add_many([record(text="login pwd=xyz9")])
        self.assertEqual(store.list()[0].text, "login pwd=<redacted>")
        # a value learned later is masked when an older record is read back
        store.add_many([record(text="later token=abcdef123456")])
        store.add_many([record(text="echo abcdef123456")])
        self.assertEqual(store.list()[2].text, "echo <redacted>")

    def test_find_dialog_matches_text_within_window(self):
        store = ConsoleStore(Redactor())
        store.add_many([record("dialog-open", "alert", "Hello alert", age=5),
                        record("dialog-open", "alert", "Old alert", age=60)])
        self.assertEqual(store.find_dialog("Hello alert").text, "Hello alert")
        self.assertEqual(store.find_dialog("Hello   alert\r\n").text, "Hello alert")
        self.assertIsNone(store.find_dialog("Old alert"))
        self.assertEqual(store.find_dialog("Old alert", within_s=120).text, "Old alert")
        self.assertIsNone(store.find_dialog("Other"))

    def test_find_dialog_only_looks_at_dialog_open_records(self):
        store = ConsoleStore(Redactor())
        store.add_many([record("dialog", "alert", "Hello alert"), record("console", "log", "Hello alert")])
        self.assertIsNone(store.find_dialog("Hello alert"))

    def test_a_dialog_is_closed_by_its_id_whatever_route_each_record_came_by(self):
        # The real sequence: the open record is drained over COM (where hwnd:1), the close by the
        # next driver call (where driver). They are the same dialog: it is no longer pending.
        store = ConsoleStore(Redactor())
        self.assertIsNone(store.pending_dialog())
        store.add_many([record("dialog-open", "confirm", "Sure?", where="hwnd:1", age=5,
                               dialog_id="a.1")])
        self.assertEqual(store.pending_dialog().text, "Sure?")
        # a close of another dialog does not close it
        store.add_many([record("dialog", "confirm", "Other -> true", where="hwnd:1",
                               dialog_id="b.7")])
        self.assertEqual(store.pending_dialog().text, "Sure?")
        store.add_many([record("dialog", "confirm", "Sure? -> true", where="driver",
                               dialog_id="a.1")])
        self.assertIsNone(store.pending_dialog())
        # a newer dialog is pending again
        store.add_many([record("dialog-open", "alert", "Again", where="driver", dialog_id="a.2")])
        self.assertEqual(store.pending_dialog().text, "Again")

    def test_frames_do_not_hide_a_close(self):
        store = ConsoleStore(Redactor())
        store.add_many([record("dialog-open", "alert", "In a frame", where="hwnd:1/0", dialog_id="f.1"),
                        record("dialog", "alert", "In a frame", where="driver/0", dialog_id="f.1")])
        self.assertIsNone(store.pending_dialog())

    def test_a_record_without_a_dialog_id_is_never_pending(self):
        store = ConsoleStore(Redactor())
        store.add_many([record("dialog-open", "alert", "No id")])
        self.assertIsNone(store.pending_dialog())

    def test_one_record_authorises_one_accept(self):
        store = ConsoleStore(Redactor())
        store.add_many([record("dialog-open", "confirm", "Sure?", dialog_id="a.1")])
        self.assertEqual(store.take_pending_dialog().dialog_id, "a.1")
        self.assertIsNone(store.take_pending_dialog())
        self.assertIsNone(store.pending_dialog())
        store.clear()  # a cleared record cannot be drained back into use
        store.add_many([record("dialog-open", "confirm", "Sure?", dialog_id="a.1")])
        self.assertIsNone(store.take_pending_dialog())

    def test_the_newest_open_dialog_is_the_pending_one(self):
        store = ConsoleStore(Redactor())
        store.add_many([record("dialog-open", "alert", "First", dialog_id="a.1"),
                        record("dialog-open", "alert", "Second", dialog_id="a.2")])
        self.assertEqual(store.take_pending_dialog().text, "Second")
        self.assertEqual(store.take_pending_dialog().text, "First")

    def test_pending_dialog_ignores_old_records(self):
        store = ConsoleStore(Redactor())
        store.add_many([record("dialog-open", "alert", "Stale", age=45, dialog_id="s.1")])
        self.assertIsNone(store.pending_dialog())
        self.assertEqual(store.pending_dialog(within_s=60).text, "Stale")

    def test_find_dialog_matches_a_popups_dialog_text(self):
        store = ConsoleStore(Redactor())
        store.add_many([record("dialog-open", "alert", "From a popup", dialog_id="p.1", popup=True)])
        self.assertEqual(store.find_dialog("From a popup").text, "From a popup")


class IngestTests(ConsoleTestCase):
    def test_xhr_records_become_page_network_entries(self):
        network = CaptureStore(Redactor())
        self.patch(consoletools, "NETWORK_STORE", network)
        raw = ("\x1fxhr\x1f\x1f1.0\x1fPOST https://h.example/api/x?a=1 status=201 resp_len=42 body=q=1&r=2\x1e"
               "\x1fxhr\x1f\x1f2.0\x1fGET /rel status=0 resp_len=0")
        consoletools.ingest(raw, "driver")
        entries = network.list()
        self.assertEqual([(e.source, e.method, e.url, e.status) for e in entries],
                         [("page", "POST", "https://h.example/api/x?a=1", 201),
                          ("page", "GET", "/rel", 0)])
        self.assertEqual(entries[0].req_body, b"q=1&r=2")
        # they are console records too
        self.assertEqual([r.kind for r in self.store.list()], ["xhr", "xhr"])

    def test_xhr_without_a_network_store_stays_a_console_record(self):
        consoletools.ingest("\x1fxhr\x1f\x1f1.0\x1fGET /a status=200 resp_len=1", "driver")
        self.assertEqual(len(self.store.list()), 1)

    def test_an_unparsable_xhr_record_is_kept_and_not_forwarded(self):
        network = CaptureStore(Redactor())
        self.patch(consoletools, "NETWORK_STORE", network)
        consoletools.ingest("\x1fxhr\x1f\x1f1.0\x1fgarbage", "driver")
        self.assertEqual(network.list(), [])
        self.assertEqual(len(self.store.list()), 1)

    def test_network_text_is_redacted_by_the_capture_store_not_the_console_text(self):
        network = CaptureStore(Redactor())
        self.patch(consoletools, "NETWORK_STORE", network)
        consoletools.ingest("\x1fxhr\x1f\x1f1.0\x1fPOST /login status=200 resp_len=1 body=user=a&pwd=hunter22", "d")
        self.assertNotIn("hunter22", self.store.list()[0].text)
        self.assertNotIn(b"hunter22", network.list()[0].req_body)


class HookTests(ConsoleTestCase):
    def test_hook_is_one_driver_call(self):
        driver = ScriptDriver("\x1fconsole\x1flog\x1f1.0\x1fhello\x1e0/1\x1ferror\x1ferror\x1f2.0\x1fboom")
        self.patch(SESSION, "driver", driver)
        consoletools.instrument_hook(SPEC, {}, protocol.text_result("ok"))
        self.assertEqual(len(driver.calls), 1)
        script, args = driver.calls[0]
        self.assertEqual(script, js.load("hook.js"))
        self.assertEqual(args[0], INSTRUMENT_SOURCE)
        self.assertEqual(args[1], {"stub": False, "wrap_xhr": False})
        self.assertEqual([(r.where, r.kind, r.text) for r in self.store.list()],
                         [("driver", "console", "hello"), ("driver/0/1", "error", "boom")])

    def test_hook_passes_the_stub_and_xhr_flags(self):
        driver = ScriptDriver()
        self.patch(SESSION, "driver", driver)
        self.settings(console_stub=True)
        self.patch(SESSION, "capture_active", False)
        consoletools.instrument_hook(SPEC, {}, protocol.text_result("ok"))
        self.assertEqual(driver.calls[0][1][1], {"stub": True, "wrap_xhr": True})
        self.assertEqual(consoletools.LAST_FLAGS, {"stub": True, "wrap_xhr": True})

    def test_wrap_xhr_follows_capture_off_and_never_a_running_proxy(self):
        self.patch(SESSION, "capture_active", None)
        self.assertFalse(consoletools.page_flags()["wrap_xhr"])
        self.patch(SESSION, "capture_active", True)
        self.assertFalse(consoletools.page_flags()["wrap_xhr"])
        self.patch(SESSION, "capture_active", False)
        self.assertTrue(consoletools.page_flags()["wrap_xhr"])
        self.patch(SESSION, "capture_active", None)
        self.patch(config, "SETTINGS", mock.Mock(console_stub=False, instrument=True, capture=False))
        self.assertTrue(consoletools.page_flags()["wrap_xhr"])

    def test_hook_without_a_driver_does_nothing(self):
        self.patch(SESSION, "driver", None)
        consoletools.instrument_hook(SPEC, {}, protocol.text_result("ok"))
        self.assertEqual(self.store.list(), [])
        self.assertEqual(consoletools.INSTALLS, 0)

    def test_a_failing_hook_script_is_not_an_install_and_does_not_raise_into_the_call(self):
        class Broken(ScriptDriver):
            def execute_script(self, *a):
                raise RuntimeError("Unable to get browser")
        self.patch(SESSION, "driver", Broken())
        with self.assertRaises(RuntimeError):  # the protocol logs it; the hook is not to hide it
            consoletools.instrument_hook(SPEC, {}, protocol.text_result("ok"))
        self.assertEqual(consoletools.INSTALLS, 0)

    def test_instrumentation_off_makes_both_hooks_return_at_once(self):
        self.settings(instrument=False)
        driver = ScriptDriver("\x1fconsole\x1flog\x1f1.0\x1fhello")
        self.patch(SESSION, "driver", driver)
        self.set_modal(info(7, "html_dialog", "Internet Explorer_TridentDlgFrame", "Lookup"))
        consoletools.instrument_hook(SPEC, {}, protocol.text_result("ok"))
        consoletools.dialog_drain(SPEC, {"target": "hwnd:5"}, protocol.text_result("ok"))
        self.assertEqual(driver.calls, [])
        self.assertEqual(FakeCom.instances, [])
        self.assertEqual(self.store.list(), [])

    def test_registering_twice_does_not_double_the_hooks(self):
        consoletools.register_hooks()
        consoletools.register_hooks()
        self.assertEqual(protocol.POST_HOOKS.count(consoletools.instrument_hook), 1)
        self.assertEqual(protocol.AFTER_CALL.count(consoletools.dialog_drain), 1)


class RegistrationTests(unittest.TestCase):
    def test_the_hooks_are_registered(self):
        # (a plain TestCase: the window-tool fakes swap the hook lists out)
        self.assertIn(consoletools.instrument_hook, protocol.POST_HOOKS)
        self.assertIn(consoletools.dialog_drain, protocol.AFTER_CALL)


class DialogDrainTests(ConsoleTestCase):
    def test_dialog_drain_uses_com_backend(self):
        FakeCom.raw = "\x1fconsole\x1flog\x1f1.0\x1finside the dialog"
        self.set_modal(info(7, "html_dialog", "Internet Explorer_TridentDlgFrame", "Lookup"))
        consoletools.dialog_drain(SPEC, {}, protocol.text_result("ok"))
        self.assertEqual([(c.hwnd, c.kind) for c in FakeCom.instances], [(7, "hwnd")])
        script, frame, args = FakeCom.instances[0].scripts[0]
        self.assertEqual(script, js.load("drain.js"))
        self.assertIsNone(frame)
        self.assertEqual(args, [INSTRUMENT_SOURCE, False, False])
        self.assertEqual([(r.where, r.text) for r in self.store.list()],
                         [("hwnd:7", "inside the dialog")])
        self.assertEqual(consoletools.INSTALLS, 1)

    def test_dialog_drain_drains_attached_tab(self):
        self.patch(backends, "DEFAULT_TARGET", "tab:9")
        FakeCom.raw = "\x1fconsole\x1flog\x1f1.0\x1fin the tab"
        consoletools.dialog_drain(SPEC, {}, protocol.text_result("ok"))
        self.assertEqual([(c.hwnd, c.kind) for c in FakeCom.instances], [(9, "tab")])
        self.assertEqual(FakeCom.instances[0].scripts[0][0], js.load("drain.js"))
        self.assertEqual(self.store.list()[0].where, "tab:9")

    def test_a_driver_default_target_drains_nothing(self):
        for target in (None, "driver", "hwnd:4"):
            with self.subTest(target=target):
                self.patch(backends, "DEFAULT_TARGET", target)
                consoletools.dialog_drain(SPEC, {}, protocol.text_result("ok"))
        self.assertEqual(FakeCom.instances, [])

    def test_an_explicit_com_target_is_drained_even_when_the_call_deferred_its_effect(self):
        # Ruling (Task 12): a queued COM click's records are drained at once and on the next call.
        for target, kind in (("hwnd:5", "hwnd"), ("tab:6", "tab")):
            with self.subTest(target=target):
                FakeCom.instances.clear()
                args = {"by": "id", "value": "x", "target": target, "mode": "async"}
                consoletools.dialog_drain(SPEC, args, protocol.text_result("ok"))
                self.assertEqual([(c.hwnd, c.kind) for c in FakeCom.instances],
                                 [(int(target.split(":")[1]), kind)])

    def test_an_explicit_driver_target_drains_nothing(self):
        consoletools.dialog_drain(SPEC, {"target": "driver"}, protocol.text_result("ok"))
        self.assertEqual(FakeCom.instances, [])

    def test_the_tools_that_close_their_target_do_not_drain_it(self):
        for name in ("ie_close_window", "ie_dialog"):
            with self.subTest(tool=name):
                consoletools.dialog_drain(protocol.REGISTRY[name], {"target": "hwnd:5"},
                                          protocol.text_result("ok"))
        self.assertEqual(FakeCom.instances, [])

    def test_one_target_is_drained_once(self):
        self.patch(backends, "DEFAULT_TARGET", "tab:9")
        self.set_modal(info(7, "html_dialog", "Internet Explorer_TridentDlgFrame", "Lookup"))
        consoletools.dialog_drain(SPEC, {"target": "hwnd:7"}, protocol.text_result("ok"))
        self.assertEqual(sorted((c.hwnd, c.kind) for c in FakeCom.instances),
                         [(7, "hwnd"), (9, "tab")])

    def test_a_script_dialog_in_a_modal_drains_the_modal_window(self):
        frame = info(10, "html_dialog", windows.DIALOG_FRAME_CLASS, "Lookup")
        alert = alert_info(11, owner=10, owner_cls=windows.DIALOG_FRAME_CLASS)
        self.infos = [frame, alert]
        self.set_modal(frame, alert)
        consoletools.dialog_drain(SPEC, {}, protocol.text_result("ok"))
        self.assertEqual(sorted(c.hwnd for c in FakeCom.instances), [10])  # once, as the html_dialog

    def test_a_page_alert_drains_the_sessions_page_windows(self):
        main = info(1, "main", title="Page")
        popup = info(2, "popup", title="Pop")
        attached = info(3, "popup", title="User tab")
        helper_alert = alert_info(21, owner=20, owner_cls=windows.HELPER_CLASS)
        self.infos = [main, popup, attached, helper_alert]
        self.patch(windows, "ATTACHED_TOPS", {3})
        self.set_modal(helper_alert)
        consoletools.dialog_drain(SPEC, {}, protocol.text_result("ok"))
        self.assertEqual(sorted(c.hwnd for c in FakeCom.instances), [1, 2])

    def test_a_script_dialog_owned_by_an_attached_top_is_left_to_the_tab_drain(self):
        owned = alert_info(31, owner=3, owner_cls="Chrome_WidgetWin_1")
        self.infos = [info(1, "main", title="Page"), owned]
        self.patch(windows, "ATTACHED_TOPS", {3})
        self.set_modal(owned)
        consoletools.dialog_drain(SPEC, {}, protocol.text_result("ok"))
        self.assertEqual(FakeCom.instances, [])

    def test_a_system_dialog_is_not_drained(self):
        self.set_modal(info(40, "system_dialog", "#32770", "Save as"))
        consoletools.dialog_drain(SPEC, {}, protocol.text_result("ok"))
        self.assertEqual(FakeCom.instances, [])

    def test_a_rejected_com_call_is_skipped_and_the_other_windows_still_drain(self):
        frame1 = info(7, "html_dialog", windows.DIALOG_FRAME_CLASS, "A")
        frame2 = info(8, "html_dialog", windows.DIALOG_FRAME_CLASS, "B")
        self.set_modal(frame1, frame2)
        FakeCom.fail = True
        consoletools.dialog_drain(SPEC, {}, protocol.text_result("ok"))  # no exception
        self.assertEqual(sorted(c.hwnd for c in FakeCom.instances), [7, 8])
        self.assertEqual(consoletools.INSTALLS, 0)

    def test_the_drain_runs_after_every_call_through_the_protocol(self):
        self.set_modal(info(7, "html_dialog", windows.DIALOG_FRAME_CLASS, "Lookup"))
        FakeCom.raw = "\x1fconsole\x1flog\x1f1.0\x1fseen"
        protocol.AFTER_CALL = [consoletools.dialog_drain]
        result = call("ie_sleep", {"seconds": 0})
        self.assertFalse(result.get("isError"), result)
        self.assertEqual([r.text for r in self.store.list()], ["seen"])


class ConsoleToolTests(ConsoleTestCase):
    def test_ie_console_lists_and_filters_records(self):
        self.store.add_many([record("error", "error", "e1"), record("console", "log", "c1"),
                             record("console", "warn", "c2")])
        spec = protocol.REGISTRY["ie_console"]
        self.assertFalse(spec.needs_driver({}))
        everything = json.loads(first_text(call("ie_console")))
        self.assertEqual([r["text"] for r in everything["records"]], ["e1", "c1", "c2"])
        self.assertEqual(everything["next_since_id"], 3)
        self.assertEqual(set(everything["records"][0]),
                         {"id", "kind", "level", "time", "text", "where"})
        some = json.loads(first_text(call("ie_console", {"since_id": 1, "kinds": ["console"],
                                                         "limit": 1})))
        self.assertEqual([r["text"] for r in some["records"]], ["c1"])
        # a comma separated string works too
        both = json.loads(first_text(call("ie_console", {"kinds": "error,console"})))
        self.assertEqual(len(both["records"]), 3)

    def test_ie_console_keeps_next_since_id_when_nothing_is_new(self):
        self.store.add_many([record(text="a")])
        got = json.loads(first_text(call("ie_console", {"since_id": 1})))
        self.assertEqual((got["records"], got["next_since_id"]), ([], 1))

    def test_ie_console_says_when_instrumentation_is_off(self):
        self.settings(instrument=False)
        got = json.loads(first_text(call("ie_console")))
        self.assertEqual(got["records"], [])
        self.assertIn("IE_INSTRUMENT=0", got["note"])

    def test_ie_console_rejects_bad_arguments(self):
        for args in ({"since_id": "x"}, {"limit": 0}, {"kinds": 5}, {"limit": True}):
            with self.subTest(args=args):
                self.assertTrue(call("ie_console", args).get("isError"))

    def test_ie_console_clear(self):
        self.store.add_many([record(text="a"), record(text="b")])
        spec = protocol.REGISTRY["ie_console_clear"]
        self.assertFalse(spec.needs_driver({}))
        self.assertEqual(json.loads(first_text(call("ie_console_clear")))["cleared"], 2)
        self.assertEqual(self.store.list(), [])

    def test_status_reports_instrumentation(self):
        info_ = json.loads(first_text(call("ie_status")))
        self.assertEqual((info_["instrumented"], info_["console_stub"], info_["xhr_wrapped"]),
                         (False, False, False))
        self.patch(SESSION, "driver", ScriptDriver())
        consoletools.instrument_hook(SPEC, {}, protocol.text_result("ok"))
        info_ = json.loads(first_text(call("ie_status")))
        self.assertTrue(info_["instrumented"])
        # a closed session is not instrumented, and neither is a new browser until a hook ran
        self.patch(SESSION, "driver", None)
        self.assertFalse(json.loads(first_text(call("ie_status")))["instrumented"])
        self.patch(SESSION, "driver", ScriptDriver())
        self.patch(SESSION, "driver_pid", 4242)
        self.assertFalse(json.loads(first_text(call("ie_status")))["instrumented"])
        consoletools.instrument_hook(SPEC, {}, protocol.text_result("ok"))
        self.assertTrue(json.loads(first_text(call("ie_status")))["instrumented"])
        self.patch(SESSION, "driver", None)
        self.settings(console_stub=True)
        self.assertTrue(json.loads(first_text(call("ie_status")))["console_stub"])
        # with IE_INSTRUMENT=0 nothing is applied to a page, whatever the stub setting says
        self.settings(instrument=False, console_stub=True)
        info_ = json.loads(first_text(call("ie_status")))
        self.assertEqual((info_["instrumented"], info_["console_stub"], info_["xhr_wrapped"]),
                         (False, False, False))

    def test_status_reports_the_xhr_flag_last_sent_to_pages(self):
        self.patch(SESSION, "driver", ScriptDriver())
        self.patch(SESSION, "capture_active", False)
        consoletools.instrument_hook(SPEC, {}, protocol.text_result("ok"))
        self.patch(SESSION, "driver", None)
        self.assertTrue(json.loads(first_text(call("ie_status")))["xhr_wrapped"])


class SettingsTests(unittest.TestCase):
    def load(self, **env):
        with tempfile.TemporaryDirectory() as tmp:
            return config.load_settings(env, pid=1, home=Path(tmp))

    def test_defaults(self):
        settings = self.load()
        self.assertTrue(settings.instrument)
        self.assertFalse(settings.console_stub)

    def test_switches(self):
        self.assertFalse(self.load(IE_INSTRUMENT="0").instrument)
        self.assertFalse(self.load(IE_INSTRUMENT="off").instrument)
        self.assertTrue(self.load(IE_INSTRUMENT="1").instrument)
        self.assertTrue(self.load(IE_INSTRUMENT="typo").instrument)  # a typo cannot turn it off
        self.assertTrue(self.load(IE_CONSOLE_STUB="1").console_stub)
        self.assertTrue(self.load(IE_CONSOLE_STUB="yes").console_stub)
        self.assertFalse(self.load(IE_CONSOLE_STUB="0").console_stub)


class TaskDialogAcceptTests(ConsoleTestCase):
    """Ruling (Task 12, amended): a TaskDialog is accepted only when the owner document reports a
    dialog of the page's own in flight AND an unused, unclosed ``dialog-open`` record exists."""

    def setUp(self):
        super().setUp()
        FakeCom.raw = state(1)  # the owner document reports one dialog in flight

    def open_taskdialog(self, hwnd=81):
        self.dialog(alert_info(hwnd), TASKDIALOG_CHILDREN)
        self.infos.append(info(1, "main", title="Page"))
        return hwnd

    def accept(self, hwnd):
        return call("ie_dialog", {"target": f"hwnd:{hwnd}", "action": "accept"})

    def opened(self, text="Leave?", dialog_id="a.1", where="hwnd:1", age=3):
        self.store.add_many([record("dialog-open", "confirm", text, where=where, age=age,
                                    dialog_id=dialog_id)])

    def test_taskdialog_accept_with_a_pending_record_is_sent(self):
        hwnd = self.open_taskdialog()
        self.opened()
        result = self.accept(hwnd)
        self.assertFalse(result.get("isError"), result)
        self.assertEqual(self.posted, [(hwnd, WM_COMMAND, 1, 0)])
        answer = json.loads(first_text(result))
        self.assertEqual(answer["evidence"]["text"], "Leave?")
        self.assertEqual(answer["evidence"]["dialog_id"], "a.1")

    def test_taskdialog_accept_without_a_record_is_refused_and_nothing_is_posted(self):
        hwnd = self.open_taskdialog()
        result = self.accept(hwnd)
        self.assertTrue(is_error(result, "ask the user"), result)
        self.assertIn("dialog-open", first_text(result))
        self.assertEqual(self.posted, [])

    def test_taskdialog_accept_with_instrumentation_off_is_refused(self):
        self.settings(instrument=False)
        hwnd = self.open_taskdialog()
        self.opened()
        result = self.accept(hwnd)
        self.assertTrue(is_error(result, "ask the user"), result)
        self.assertIn("IE_INSTRUMENT=0", first_text(result))
        self.assertEqual(self.posted, [])
        self.assertEqual(FakeCom.instances, [])

    def test_the_real_sequence_open_drained_over_com_close_drained_by_the_driver_is_refused(self):
        # An alert is answered: its open record came in as hwnd:1 (COM), its close record as
        # driver (the next call's hook). A later helper-owned TaskDialog (an onbeforeunload
        # prompt, say) must not be accepted on the strength of that old open record.
        hwnd = self.open_taskdialog()
        self.opened("Hello alert", dialog_id="k.1", where="hwnd:1", age=4)
        self.store.add_many([record("dialog", "alert", "Hello alert", where="driver", age=2,
                                    dialog_id="k.1")])
        result = self.accept(hwnd)
        self.assertTrue(is_error(result, "ask the user"), result)
        self.assertEqual(self.posted, [])

    def test_a_record_used_once_refuses_a_second_accept(self):
        hwnd = self.open_taskdialog()
        self.opened()
        self.assertFalse(self.accept(hwnd).get("isError"))
        self.posted.clear()
        self.gone = False
        self.assertTrue(is_error(self.accept(hwnd), "ask the user"))
        self.assertEqual(self.posted, [])

    def test_a_live_counter_of_zero_refuses_even_with_a_pending_record(self):
        hwnd = self.open_taskdialog()
        self.opened()
        FakeCom.raw = state(0)
        result = self.accept(hwnd)
        self.assertTrue(is_error(result, "ask the user"), result)
        self.assertIn("no alert, confirm or prompt", first_text(result))
        self.assertEqual(self.posted, [])
        self.assertIsNotNone(self.store.pending_dialog())  # a refusal does not use the record up

    def test_a_failed_drain_refuses_even_with_a_pending_record(self):
        hwnd = self.open_taskdialog()
        self.opened()
        FakeCom.fail = True
        self.assertTrue(is_error(self.accept(hwnd), "ask the user"))
        self.assertEqual(self.posted, [])
        FakeCom.fail = False
        self.assertFalse(self.accept(hwnd).get("isError"))  # the record was still there

    def test_a_drain_that_reports_no_state_refuses(self):
        # (an uninstrumented document returns nothing at all)
        hwnd = self.open_taskdialog()
        self.opened()
        FakeCom.raw = ""
        self.assertTrue(is_error(self.accept(hwnd), "could not be read"))
        self.assertEqual(self.posted, [])

    def test_no_reachable_owner_document_refuses(self):
        hwnd = 83
        self.dialog(alert_info(hwnd), TASKDIALOG_CHILDREN)  # no page window in the session
        self.opened()
        self.assertTrue(is_error(self.accept(hwnd), "ask the user"))
        self.assertEqual(FakeCom.instances, [])
        self.assertEqual(self.posted, [])

    def test_the_counters_of_all_candidate_windows_are_added_and_any_failure_refuses(self):
        hwnd = self.open_taskdialog()
        self.infos.append(info(2, "popup", title="Pop"))
        self.opened()
        FakeCom.raw = state(0)
        self.assertTrue(is_error(self.accept(hwnd), "ask the user"))
        self.assertEqual(sorted(c.hwnd for c in FakeCom.instances), [1, 2])

    def test_a_dialog_inside_a_modal_window_reads_the_modal_documents_counter(self):
        frame = info(10, "html_dialog", windows.DIALOG_FRAME_CLASS, "Lookup")
        dialog = alert_info(11, owner=10, owner_cls=windows.DIALOG_FRAME_CLASS)
        self.infos = [info(1, "main", title="Page"), frame]
        self.dialog(dialog, TASKDIALOG_CHILDREN)
        self.opened(where="hwnd:10")
        result = self.accept(11)
        self.assertFalse(result.get("isError"), result)
        self.assertEqual([c.hwnd for c in FakeCom.instances], [10])  # the dialog frame only

    def test_a_record_older_than_30_seconds_is_refused(self):
        hwnd = self.open_taskdialog()
        self.opened(age=45)
        self.assertTrue(is_error(self.accept(hwnd), "ask the user"))
        self.assertEqual(self.posted, [])

    def test_ie_dialog_drains_the_owner_document_before_looking(self):
        # The record only exists once the page's queue has been drained: ie_dialog does that
        # itself before it decides, and the same drain reports the live counter.
        hwnd = self.open_taskdialog()
        FakeCom.raw = (f"\x1fdialog-open\x1fconfirm#z.1\x1f{time.time()}\x1fLeave?\x1e"
                       + state(1))
        result = self.accept(hwnd)
        self.assertFalse(result.get("isError"), result)
        self.assertEqual([c.hwnd for c in FakeCom.instances], [1])
        self.assertEqual(self.posted, [(hwnd, WM_COMMAND, 1, 0)])
        self.assertEqual(json.loads(first_text(result))["evidence"]["dialog_id"], "z.1")

    def test_a_close_record_in_the_drain_refuses_the_accept(self):
        hwnd = self.open_taskdialog()
        self.opened(where="driver", age=5)  # a different route than the drain's hwnd:1
        FakeCom.raw = f"\x1fdialog\x1fconfirm#a.1\x1f{time.time()}\x1fLeave? -> true\x1e" + state(1)
        self.assertTrue(is_error(self.accept(hwnd), "ask the user"))
        self.assertEqual(self.posted, [])

    def test_state_records_are_not_stored_as_console_records(self):
        hwnd = self.open_taskdialog()
        self.opened()
        self.accept(hwnd)
        self.assertEqual([r.kind for r in self.store.list()], ["dialog-open"])

    def test_dismiss_of_a_taskdialog_needs_no_record(self):
        hwnd = self.open_taskdialog()
        FakeCom.raw = ""
        result = call("ie_dialog", {"target": f"hwnd:{hwnd}", "action": "dismiss"})
        self.assertFalse(result.get("isError"), result)
        self.assertEqual(self.posted, [(hwnd, WM_COMMAND, 2, 0)])
        self.assertEqual(FakeCom.instances, [])

    def test_classic_dialogs_need_no_record_and_get_the_matching_one_as_evidence(self):
        self.dialog(alert_info(70, owner=9), alert_children(70))
        self.texts[73] = "Hello alert"
        self.store.add_many([record("dialog-open", "alert", "Hello alert", where="hwnd:1", age=2)])
        result = call("ie_dialog", {"target": "hwnd:70", "action": "accept"})
        self.assertFalse(result.get("isError"), result)
        self.assertEqual(self.posted, [(70, WM_COMMAND, 2, 0)])
        self.assertEqual(json.loads(first_text(result))["evidence"]["kind"], "dialog-open")

    def test_a_classic_dialog_without_a_record_has_no_evidence(self):
        self.dialog(alert_info(71, owner=9), alert_children(71))
        self.texts[74] = "Unrecorded"
        answer = json.loads(first_text(call("ie_dialog", {"target": "hwnd:71"})))
        self.assertIsNone(answer.get("evidence"))
        self.assertEqual(self.posted, [(71, WM_COMMAND, 2, 0)])

    def test_the_driver_alert_answer_carries_the_matching_record_too(self):
        class Alert:
            text = "Hello alert"

            def accept(self):
                pass

        driver = self.use_driver(WindowDriver({"H1": ("Page", "http://x/")}))
        driver.alert_obj = Alert()
        self.store.add_many([record("dialog-open", "alert", "Hello alert", where="driver", age=1)])
        answer = json.loads(first_text(call("ie_dialog", {})))
        self.assertEqual((answer["did"], answer["evidence"]["text"]), ("accepted", "Hello alert"))

    def test_a_failing_record_lookup_never_turns_an_answer_into_an_error(self):
        self.dialog(alert_info(72, owner=9), alert_children(72))
        self.texts[75] = "Hello alert"
        self.patch(self.store, "find_dialog", mock.Mock(side_effect=RuntimeError("boom")))
        answer = json.loads(first_text(call("ie_dialog", {"target": "hwnd:72"})))
        self.assertEqual(answer["did"], "accepted")
        self.assertNotIn("evidence", answer)
        self.assertEqual(self.posted, [(72, WM_COMMAND, 2, 0)])

    def test_the_no_target_owned_window_path_has_the_same_rule(self):
        hwnd = self.open_taskdialog(82)
        self.set_modal(self.infos[0])
        self.use_driver(WindowDriver({"H1": ("Page", "http://x/")}))  # it has no alert open
        refused = call("ie_dialog", {})
        self.assertTrue(is_error(refused, "ask the user"), refused)
        self.assertEqual(self.posted, [])
        self.opened()
        self.assertFalse(call("ie_dialog", {}).get("isError"))
        self.assertEqual(self.posted, [(hwnd, WM_COMMAND, 1, 0)])


class ResourceTests(unittest.TestCase):
    def test_instrumentation_scripts_exist_and_are_wired(self):
        for name in ("instrument.js", "hook.js", "drain.js"):
            self.assertTrue(js.load(name).strip(), name)

    def test_instrument_js_keeps_the_documented_record_kinds(self):
        text = INSTRUMENT_SOURCE
        for kind in ("error", "dialog-open", "'dialog'", "modal", "open", "console", "xhr"):
            self.assertIn(kind, text)
        for flag in ("__iemcp_stub", "__iemcp_wrap_xhr", "__iemcp"):
            self.assertIn(flag, text)

    def test_the_scripts_never_wrap_activex(self):
        for name in ("instrument.js", "hook.js", "drain.js"):
            self.assertNotIn("ActiveXObject", js.load(name), name)


if __name__ == "__main__":
    unittest.main()
