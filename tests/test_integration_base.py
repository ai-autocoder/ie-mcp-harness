"""The integration test base must leave nothing behind, even when a test (or ie_close) fails.

Fakes only: no browser. This pins the teardown logic that real runs depend on.
"""
import unittest
from unittest import mock

from ie_mcp import protocol
from ie_mcp.session import SESSION
from tests.integration import base

DRIVER = (101, ("iedriverserver.exe", 1))
EDGE = (102, ("msedge.exe", 2))
OLD_EDGE = (90, ("msedge.exe", 3))  # a browser the session recreated away from


class FinishSessionTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        for target, name, value in (
                (protocol, "handle", self.fake_handle),
                (SESSION, "close", lambda: self.calls.append("close")),
                (base, "kill_verified", lambda targets: self.calls.append(("kill", targets)) or []),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.alive = set()
        patcher = mock.patch.object(base, "_same_process",
                                    lambda pid, identity: pid in self.alive)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.close_raises = None

    def fake_handle(self, message):
        self.calls.append(message["params"]["name"])
        if self.close_raises:
            raise self.close_raises
        return {"result": {}}

    def test_clean_close_reports_nothing(self):
        self.assertEqual(base.finish_session([DRIVER, EDGE]), [])
        self.assertEqual(self.calls, ["ie_close", "close"])

    def test_session_close_runs_even_when_ie_close_fails(self):
        self.close_raises = RuntimeError("driver hung")
        problems = base.finish_session([DRIVER, EDGE])
        self.assertEqual(self.calls, ["ie_close", "close"])
        self.assertTrue(any("driver hung" in p for p in problems), problems)

    def test_survivors_are_killed_by_recorded_identity_and_reported(self):
        # The old browser of a recreated session is no longer SESSION's, but it was recorded.
        self.alive = {EDGE[0], OLD_EDGE[0]}
        problems = base.finish_session([OLD_EDGE, DRIVER, EDGE])
        self.assertEqual(self.calls[-1], ("kill", [OLD_EDGE, EDGE]))
        self.assertTrue(any("still running" in p for p in problems), problems)

    def test_processes_are_remembered_across_session_recreation(self):
        class Case(base.IntegrationTestCase):
            def runTest(self):
                pass

        Case.recorded_processes = []
        with mock.patch.object(SESSION, "driver_pid", 101), \
                mock.patch.object(SESSION, "driver_identity", DRIVER[1]), \
                mock.patch.object(SESSION, "edge_pid", 90), \
                mock.patch.object(SESSION, "edge_identity", OLD_EDGE[1]):
            Case.remember_processes()
        with mock.patch.object(SESSION, "driver_pid", 101), \
                mock.patch.object(SESSION, "driver_identity", DRIVER[1]), \
                mock.patch.object(SESSION, "edge_pid", 102), \
                mock.patch.object(SESSION, "edge_identity", EDGE[1]):
            Case.remember_processes()
        self.assertEqual(Case.recorded_processes, [DRIVER, OLD_EDGE, EDGE])

    def test_a_process_without_a_recorded_identity_is_never_remembered(self):
        class Case(base.IntegrationTestCase):
            def runTest(self):
                pass

        Case.recorded_processes = []
        with mock.patch.object(SESSION, "driver_pid", 101), \
                mock.patch.object(SESSION, "driver_identity", None), \
                mock.patch.object(SESSION, "edge_pid", None):
            Case.remember_processes()
        self.assertEqual(Case.recorded_processes, [])


if __name__ == "__main__":
    unittest.main()
