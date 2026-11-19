"""IeSession lifecycle: lease across retries, bounded creation, close, force close, cancel.

Everything runs against fakes: no browser starts, no real process is inspected or killed.
"""
import dataclasses
import io
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ie_mcp import config, protocol, session
import ie_mcp.tools  # noqa: F401  (importing the package registers every tool)
from ie_mcp.session import SESSION, IeSession

PROCESS_FIELDS = ("driver", "driver_pid", "edge_pid", "profile", "driver_identity",
                  "edge_identity")


def _call(name, arguments=None):
    return protocol.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": name, "arguments": arguments or {}}})


def _text(response):
    return response["result"]["content"][0]["text"]


class FakeSwitchTo:
    def default_content(self):
        pass


class FakeDriver:
    """A live-looking driver. ``quit`` can block until ``release`` is set."""

    def __init__(self, pid=10, quit_blocks=False):
        self.switch_to = FakeSwitchTo()
        self.service = SimpleNamespace(process=SimpleNamespace(pid=pid))
        self.current_url = "http://127.0.0.1/"
        self.quit_calls = 0
        self.release = threading.Event()
        self._quit_blocks = quit_blocks

    def set_page_load_timeout(self, seconds):
        pass

    def quit(self):
        self.quit_calls += 1
        if self._quit_blocks:
            self.release.wait(5)

    def find_elements(self, by, value):
        return []

    def find_element(self, by, value):
        return SimpleNamespace(text="")


class ExplodingDriver:
    """Any use at all fails: force close must never touch the driver."""

    def __getattr__(self, name):
        raise AssertionError(f"force close touched the driver: {name}")


class LifecycleTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        stderr = mock.patch("sys.stderr", io.StringIO())
        stderr.start()
        self.addCleanup(stderr.stop)

        self.paths = config.build_session_paths(
            env={"IE_SESSION_ROOT": str(self.tmp / "sessions"), "IE_SESSION_ID": "t"},
            pid=os.getpid())
        original = config.SETTINGS
        config.SETTINGS = dataclasses.replace(
            original, session_paths=self.paths, use_lock=True, attach_retries=3)
        self.addCleanup(setattr, config, "SETTINGS", original)

        self.lease = session.SessionLease(
            self.paths, pid=os.getpid(), pid_alive=lambda pid: pid == os.getpid())
        self.kills = []
        self.killed_result = []

        def fake_kill_verified(targets, *a, **k):
            self.kills.append(list(targets))
            return [pid for pid, _ in targets]

        kill_patch = mock.patch.object(session, "kill_verified", side_effect=fake_kill_verified)
        self.kill_verified = kill_patch.start()
        self.addCleanup(kill_patch.stop)
        for patcher in (
            mock.patch.object(session, "LEASE", self.lease),
            mock.patch.object(session, "list_ie_browsers", return_value=[]),
            mock.patch.object(session, "process_identity",
                              side_effect=lambda pid: ("fake.exe", int(pid))),
            mock.patch.object(session, "RETRY_DELAY", 0),
            # No scan of the real desktop for the main window in these tests.
            mock.patch.object(IeSession, "_ie_tops", return_value={}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.lease.release)

    def lock_owner(self):
        return session._read_json(self.paths.lock_file).get("owner_pid")


class EnsureTests(LifecycleTestCase):
    def test_lease_is_held_across_attach_retries(self):
        s = IeSession()
        attempts = []
        good = FakeDriver()

        def builder(initial_url, **kwargs):
            attempts.append(self.lock_owner())
            if len(attempts) < 3:
                raise RuntimeError("transient attach failure")
            return good

        with mock.patch.object(s, "_build", side_effect=builder):
            driver = s.ensure(initial_url="http://127.0.0.1/")

        self.assertIs(driver, good)
        # The lock was ours before every attempt, including the retries.
        self.assertEqual(attempts, [os.getpid()] * 3)
        self.assertEqual(self.lock_owner(), os.getpid())

    def test_every_attempt_is_marked_as_starting_in_the_owner_record(self):
        # A failed attempt's teardown clears the mark; the retries must set it again.
        s = IeSession()
        marks = []

        def builder(initial_url, **kwargs):
            marks.append(session._read_json(self.paths.owner_file).get("starting_since"))
            if len(marks) < 3:
                raise RuntimeError("transient attach failure")
            return FakeDriver()

        with mock.patch.object(s, "_build", side_effect=builder):
            s.ensure(initial_url="http://127.0.0.1/")

        self.assertEqual(len(marks), 3)
        self.assertTrue(all(isinstance(m, float) for m in marks), marks)
        # once the browser is up the mark is gone
        self.assertIsNone(session._read_json(self.paths.owner_file).get("starting_since"))

    def test_failed_attempts_tear_the_browser_down_without_releasing_the_lease(self):
        s = IeSession()
        drivers = []

        def builder(initial_url, **kwargs):
            # Creation succeeds but the browser does not survive the first probe.
            driver = FakeDriver(pid=10 + len(drivers))
            driver.set_page_load_timeout = mock.Mock(side_effect=RuntimeError("lost the browser"))
            drivers.append(driver)
            return driver

        with mock.patch.object(s, "_build", side_effect=builder):
            with self.assertRaisesRegex(RuntimeError, "could not start Edge IE mode after 3"):
                s.ensure(initial_url="http://127.0.0.1/")

        # Every failed attempt quit its driver and killed its own IEDriver by identity.
        self.assertEqual([d.quit_calls for d in drivers], [1, 1, 1])
        self.assertEqual([[pid for pid, _ in call] for call in self.kills],
                         [[10], [11], [12]])
        self.assertIsNone(s.driver)
        self.assertIsNone(s.driver_pid)
        self.assertEqual(self.lock_owner(), os.getpid())

    def test_ensure_keeps_the_session_while_an_alert_is_open(self):
        # With unhandledPromptBehavior=ignore an open dialog makes driver commands raise
        # UnexpectedAlertPresentException; that is a live browser, not a dead session.
        from selenium.common.exceptions import (UnexpectedAlertPresentException,
                                                WebDriverException)

        class Failing(FakeDriver):
            error = None

            @property
            def current_url(self):
                raise self.error

            @current_url.setter
            def current_url(self, value):
                pass

        s = IeSession()
        alerted = Failing(pid=10)
        alerted.error = UnexpectedAlertPresentException("alert open")
        s.driver = alerted
        with mock.patch.object(s, "quit_browser") as quit_browser, \
                mock.patch.object(s, "_build") as build:
            self.assertIs(s.ensure(), alerted)
            quit_browser.assert_not_called()
            build.assert_not_called()

        # Any other driver failure still means the session is dead.
        dead = Failing(pid=11)
        dead.error = WebDriverException("gone")
        s.driver = dead
        with mock.patch.object(s, "quit_browser") as quit_browser, \
                mock.patch.object(s, "_build", return_value=FakeDriver(pid=12)) as build:
            s.ensure(initial_url="http://127.0.0.1/")
            quit_browser.assert_called()
            build.assert_called()

    def test_ensure_records_process_identities_in_the_owner_file(self):
        s = IeSession()
        with mock.patch.object(s, "_build", return_value=FakeDriver(pid=10)), \
                mock.patch.object(session, "list_ie_browsers", side_effect=[
                    [],  # before the attempt
                    [{"pid": 11, "kind": "edge-ie", "profile": "newp", "url": None,
                      "parent": 10}]]):
            s.ensure(initial_url="http://127.0.0.1/")

        self.assertEqual(s.driver_identity, ("fake.exe", 10))
        self.assertEqual(s.edge_identity, ("fake.exe", 11))
        record = session._read_json(self.paths.owner_file)
        self.assertEqual(record["driver_pid"], 10)
        self.assertEqual(record["edge_pid"], 11)
        self.assertEqual(record["profile"], "newp")
        self.assertEqual(tuple(record["driver_identity"]), ("fake.exe", 10))
        self.assertEqual(tuple(record["edge_identity"]), ("fake.exe", 11))

    def _capture(self, rows, before=frozenset()):
        s = IeSession()
        s.driver = FakeDriver(pid=10)
        with mock.patch.object(session, "list_ie_browsers", return_value=rows):
            s._capture_identity(before)
        return s

    def test_capture_adopts_only_a_new_child_of_our_iedriver(self):
        s = self._capture([
            {"pid": 20, "kind": "edge-ie", "profile": "mine", "url": None, "parent": 10},
        ])

        self.assertEqual((s.edge_pid, s.profile, s.edge_identity), (20, "mine", ("fake.exe", 20)))

    def test_capture_never_adopts_a_window_that_is_not_provably_ours(self):
        not_ours = {
            "hand-started edge without an IEDriver profile":
                {"pid": 20, "kind": "edge-ie", "profile": None, "url": None, "parent": 10},
            "another agent's new window (other IEDriver)":
                {"pid": 21, "kind": "edge-ie", "profile": "theirs", "url": None, "parent": 99},
            "a window that existed before this attempt":
                {"pid": 22, "kind": "edge-ie", "profile": "old", "url": None, "parent": 10},
            "a row without parent information":
                {"pid": 23, "kind": "edge-ie", "profile": "mine", "url": None},
        }
        for label, row in not_ours.items():
            with self.subTest(label):
                s = self._capture([row], before={"old"})

                self.assertIsNone(s.edge_pid)
                self.assertIsNone(s.edge_identity)
                self.assertEqual(s.driver_pid, 10)  # our own IEDriver is still recorded

    def test_capture_adopts_nothing_when_two_children_match(self):
        s = self._capture([
            {"pid": 20, "kind": "edge-ie", "profile": "a", "url": None, "parent": 10},
            {"pid": 21, "kind": "edge-ie", "profile": "b", "url": None, "parent": 10},
        ])

        self.assertIsNone(s.edge_pid)

    def test_a_force_close_during_creation_tears_the_new_browser_down(self):
        s = IeSession()
        good = FakeDriver(pid=10)

        def builder(initial_url, **kwargs):
            s.cancel.set()  # ie_close(force=true) arrives while webdriver.Ie() is running
            return good

        with mock.patch.object(s, "_build", side_effect=builder):
            with self.assertRaises(session.Cancelled):
                s.ensure(initial_url="http://127.0.0.1/")

        self.assertIsNone(s.driver)
        self.assertEqual(good.quit_calls, 1)
        self.assertEqual(self.kills, [[(10, ("fake.exe", 10))]])
        self.assertTrue(s.cancel.is_set())


class BoundedCreationTests(LifecycleTestCase):
    def test_creation_that_hangs_is_abandoned_and_only_our_processes_are_killed(self):
        from selenium import webdriver
        from selenium.webdriver.ie import service as ie_service

        driver_file = self.tmp / "IEDriverServer.exe"
        driver_file.write_bytes(b"")
        settings = dataclasses.replace(config.SETTINGS, driver_path=str(driver_file))
        late_driver = FakeDriver()
        unblock = threading.Event()

        class FakeService:
            def __init__(self, *a, **k):
                self.process = SimpleNamespace(pid=77)

        def hanging_ie(service=None, options=None):
            unblock.wait(5)
            return late_driver

        self.addCleanup(unblock.set)
        browsers = [
            {"pid": 50, "kind": "edge-ie", "profile": "someone-elses", "url": None, "parent": 77},
            {"pid": 51, "kind": "edge-ie", "profile": None, "url": None, "parent": 77},
            {"pid": 52, "kind": "edge-ie", "profile": "other-agent", "url": None, "parent": 5},
            {"pid": 88, "kind": "edge-ie", "profile": "ours", "url": None, "parent": 77},
        ]
        s = IeSession()
        with mock.patch.object(session, "SESSION_CREATE_TIMEOUT", 0.3), \
                mock.patch.object(webdriver, "Ie", hanging_ie), \
                mock.patch.object(ie_service, "Service", FakeService), \
                mock.patch.object(session, "list_ie_browsers", return_value=browsers):
            started = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                s._build("http://127.0.0.1/", settings=settings,
                         before_profiles={"someone-elses"})
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 2)
        self.assertEqual(len(self.kills), 1)
        self.assertEqual(sorted(pid for pid, _ in self.kills[0]), [77, 88])
        self.assertEqual(dict(self.kills[0])[77], ("fake.exe", 77))
        # The hung creation finally returns: its driver is quit and discarded.
        unblock.set()
        deadline = time.monotonic() + 2
        while late_driver.quit_calls == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(late_driver.quit_calls, 1)

    def _reap(self, browsers, service_pid=77):
        service = SimpleNamespace(process=SimpleNamespace(pid=service_pid))
        with mock.patch.object(session, "list_ie_browsers", return_value=browsers):
            IeSession()._reap_failed_attempt(service, set())

    def test_reap_kills_the_iedriver_and_its_single_new_child_edge(self):
        self._reap([{"pid": 88, "kind": "edge-ie", "profile": "a", "url": None, "parent": 77}])

        self.assertEqual(self.kills, [[(77, ("fake.exe", 77)), (88, ("fake.exe", 88))]])

    def test_reap_never_kills_another_agents_new_edge(self):
        # Our attempt spawned no Edge; one other agent's brand-new window is not ours.
        self._reap([{"pid": 88, "kind": "edge-ie", "profile": "a", "url": None, "parent": 5}])

        self.assertEqual(self.kills, [[(77, ("fake.exe", 77))]])

    def test_reap_kills_no_edge_when_two_children_match(self):
        self._reap([
            {"pid": 88, "kind": "edge-ie", "profile": "a", "url": None, "parent": 77},
            {"pid": 89, "kind": "edge-ie", "profile": "b", "url": None, "parent": 77},
        ])

        self.assertEqual(self.kills, [[(77, ("fake.exe", 77))]])

    def test_reap_without_a_service_kills_nothing(self):
        with mock.patch.object(session, "list_ie_browsers", return_value=[
                {"pid": 88, "kind": "edge-ie", "profile": "a", "url": None, "parent": 77}]):
            IeSession()._reap_failed_attempt(None, set())

        self.assertEqual(self.kills, [])

    def test_a_driver_that_cannot_be_quit_in_time_does_not_hang_a_failed_build(self):
        from selenium import webdriver
        from selenium.webdriver.ie import service as ie_service

        driver_file = self.tmp / "IEDriverServer.exe"
        driver_file.write_bytes(b"")
        settings = dataclasses.replace(config.SETTINGS, driver_path=str(driver_file))
        stuck = FakeDriver(quit_blocks=True)
        self.addCleanup(stuck.release.set)

        class FakeService:
            def __init__(self, *a, **k):
                self.process = SimpleNamespace(pid=77)

        with mock.patch.object(session, "QUIT_TIMEOUT", 0.2), \
                mock.patch.object(webdriver, "Ie", return_value=stuck), \
                mock.patch.object(ie_service, "Service", FakeService), \
                mock.patch.object(session, "apply_driver_timeout",
                                  side_effect=RuntimeError("no timeout for you")):
            started = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, "no timeout"):
                IeSession()._build("http://127.0.0.1/", settings=settings, before_profiles=set())

        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(stuck.quit_calls, 1)
        self.assertEqual(self.kills, [[(77, ("fake.exe", 77))]])


class CloseTests(LifecycleTestCase):
    def _owning_session(self, driver):
        s = IeSession()
        s.driver = driver
        s.driver_pid, s.driver_identity = 10, ("fake.exe", 10)
        s.edge_pid, s.edge_identity = 11, ("fake.exe", 11)
        s.profile = "p"
        self.lease.acquire()
        return s

    def test_close_kills_own_processes_when_quit_hangs(self):
        driver = FakeDriver(quit_blocks=True)
        self.addCleanup(driver.release.set)
        s = self._owning_session(driver)

        started = time.monotonic()
        s.close(timeout_s=0.2)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1)
        self.assertEqual(self.kills, [[(10, ("fake.exe", 10)), (11, ("fake.exe", 11))]])
        self.assertTrue(s.cancel.is_set())
        self.assertIsNone(s.driver)
        self.assertFalse(self.paths.lock_file.exists())  # close releases the lease

    def test_quit_browser_keeps_the_lease(self):
        driver = FakeDriver()
        s = self._owning_session(driver)

        s.quit_browser()

        self.assertEqual(driver.quit_calls, 1)
        self.assertEqual(self.kills, [[(10, ("fake.exe", 10)), (11, ("fake.exe", 11))]])
        self.assertIsNone(s.driver)
        self.assertIsNone(s.edge_pid)
        self.assertEqual(self.lock_owner(), os.getpid())
        self.assertIsNone(session._read_json(self.paths.owner_file).get("edge_identity"))

    def test_quit_browser_does_not_wait_for_a_hung_driver(self):
        driver = FakeDriver(quit_blocks=True)
        self.addCleanup(driver.release.set)
        s = self._owning_session(driver)

        with mock.patch.object(session, "QUIT_TIMEOUT", 0.2):
            started = time.monotonic()
            s.quit_browser()

        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(len(self.kills), 1)

    def test_a_failed_kill_keeps_the_record_of_a_process_that_is_still_alive(self):
        driver = FakeDriver()
        s = self._owning_session(driver)
        # taskkill failed for both, and both processes are still running.
        self.kill_verified.side_effect = lambda targets, *a, **k: []

        s.quit_browser()

        self.assertEqual((s.driver_pid, s.edge_pid), (10, 11))
        self.assertEqual(s.edge_identity, ("fake.exe", 11))
        record = session._read_json(self.paths.owner_file)
        self.assertEqual((record["driver_pid"], record["edge_pid"], record["profile"]),
                         (10, 11, "p"))
        self.assertEqual(tuple(record["edge_identity"]), ("fake.exe", 11))

    def test_a_process_that_is_gone_is_forgotten_even_if_the_kill_reported_nothing(self):
        s = self._owning_session(FakeDriver())
        self.kill_verified.side_effect = lambda targets, *a, **k: []

        with mock.patch.object(session, "process_identity", return_value=None):
            s.quit_browser()

        self.assertEqual((s.driver_pid, s.edge_pid, s.profile), (None, None, None))
        self.assertIsNone(session._read_json(self.paths.owner_file).get("edge_pid"))

    def test_only_the_survivor_is_kept(self):
        s = self._owning_session(FakeDriver())
        self.kill_verified.side_effect = lambda targets, *a, **k: [10]  # edge survives

        s.quit_browser()

        self.assertEqual((s.driver_pid, s.edge_pid), (None, 11))

    def test_close_keeps_the_lease_when_a_process_survived_the_kill(self):
        s = self._owning_session(FakeDriver())
        self.kill_verified.side_effect = lambda targets, *a, **k: []

        s.close(timeout_s=0.2)

        # The next server takes the lease over as stale and keeps this record for cleanup.
        self.assertEqual(self.lock_owner(), os.getpid())
        self.assertEqual(session._read_json(self.paths.owner_file)["edge_pid"], 11)

    def test_force_kill_during_creation_kills_the_iedriver_that_already_exists(self):
        s = IeSession()
        s._creating_service = SimpleNamespace(process=SimpleNamespace(pid=77))

        killed = s.force_kill()

        self.assertEqual(killed, [77])
        self.assertEqual(self.kills, [[(77, ("fake.exe", 77))]])

    def test_quit_no_longer_exists(self):
        self.assertFalse(hasattr(IeSession, "quit"))


class ToolLifecycleTests(LifecycleTestCase):
    def setUp(self):
        super().setUp()
        for name in PROCESS_FIELDS:
            patcher = mock.patch.object(SESSION, name, None)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cancel = threading.Event()
        patcher = mock.patch.object(SESSION, "cancel", self.cancel)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_force_close_does_not_touch_driver(self):
        SESSION.driver = ExplodingDriver()
        SESSION.driver_pid, SESSION.driver_identity = 10, ("fake.exe", 10)
        SESSION.edge_pid, SESSION.edge_identity = 11, ("fake.exe", 11)
        self.lease.acquire()

        response = _call("ie_close", {"force": True})

        self.assertNotIn("isError", response["result"], _text(response))
        self.assertEqual(self.kills, [[(10, ("fake.exe", 10)), (11, ("fake.exe", 11))]])
        self.assertTrue(self.cancel.is_set())
        self.assertIsNone(SESSION.driver)
        self.assertIsNone(SESSION.driver_pid)
        self.assertEqual(self.lock_owner(), os.getpid())  # the process still owns the identity

    def test_ie_close_schema_has_force_and_does_not_need_the_driver_when_forced(self):
        spec = protocol.REGISTRY["ie_close"]

        self.assertEqual(spec.input_schema["properties"]["force"]["type"], "boolean")
        self.assertFalse(spec.needs_driver({"force": True}))
        self.assertTrue(spec.needs_driver({}))
        self.assertIn("force", spec.description)

    def test_ie_close_without_force_quits_the_browser(self):
        driver = FakeDriver()
        SESSION.driver = driver
        SESSION.driver_pid, SESSION.driver_identity = 10, ("fake.exe", 10)
        self.lease.acquire()

        response = _call("ie_close")

        self.assertNotIn("isError", response["result"], _text(response))
        self.assertEqual(driver.quit_calls, 1)
        self.assertFalse(self.cancel.is_set())
        self.assertEqual(self.lock_owner(), os.getpid())

    def test_wait_loop_stops_when_cancelled(self):
        timer = threading.Timer(0.2, self.cancel.set)
        self.addCleanup(timer.join)
        self.addCleanup(timer.cancel)
        timer.start()

        with mock.patch.object(SESSION, "ensure", return_value=FakeDriver()):
            started = time.monotonic()
            response = _call("ie_wait_text",
                             {"contains": "never there", "timeout": 30, "interval": 0.1})
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1)
        self.assertTrue(response["result"]["isError"])
        self.assertIn("cancelled", _text(response))

    def test_every_wait_tool_checks_the_cancel_flag(self):
        self.cancel.set()
        calls = {
            "ie_wait_text": {"contains": "x", "timeout": 30, "interval": 0.1},
            "ie_wait_element": {"by": "id", "value": "x", "timeout": 30, "interval": 0.1},
            "ie_wait_ready": {"timeout": 30, "interval": 0.1},
        }
        for name, args in calls.items():
            with self.subTest(tool=name), \
                    mock.patch.object(SESSION, "ensure", return_value=FakeDriver()):
                started = time.monotonic()
                response = _call(name, args)

                self.assertLess(time.monotonic() - started, 1)
                self.assertTrue(response["result"]["isError"])
                self.assertIn("cancelled", _text(response))

    def test_find_stops_when_cancelled(self):
        self.cancel.set()
        s = SESSION

        with mock.patch.object(IeSession, "ensure", return_value=FakeDriver()), \
                mock.patch.object(IeSession, "_apply_frame"):
            started = time.monotonic()
            with self.assertRaisesRegex(session.Cancelled, "cancelled"):
                s.find("id", "x", timeout=30, interval=0.1)

        self.assertLess(time.monotonic() - started, 1)

    def test_cancel_cleared_by_next_session(self):
        # After a force close the next session starts with a clear flag, so its waits poll
        # normally (and time out normally) instead of reporting "cancelled".
        _call("ie_close", {"force": True})
        self.assertTrue(self.cancel.is_set())

        with mock.patch.object(SESSION, "_build", return_value=FakeDriver()):
            SESSION.ensure(initial_url="http://127.0.0.1/")
        self.assertFalse(self.cancel.is_set())

        response = _call("ie_wait_text",
                         {"contains": "never there", "timeout": 0.3, "interval": 0.1})

        self.assertNotIn("isError", response["result"], _text(response))
        self.assertIn("timed_out", _text(response))


if __name__ == "__main__":
    unittest.main()
