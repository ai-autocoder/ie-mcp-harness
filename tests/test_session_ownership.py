import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ie_mcp import session
from ie_mcp.win32 import processes


def _recorded_identity(pid):
    """A fake process_identity: the identity the fixtures below recorded for ``pid``."""
    names = {101: "iedriverserver.exe", 102: "iedriverserver.exe", 201: "msedge.exe",
             202: "msedge.exe", 5: "iedriverserver.exe", 6: "msedge.exe"}
    return (names[pid], pid) if pid in names else None


def _pids(targets):
    return [pid for pid, _ in targets]


class SessionPathTests(unittest.TestCase):
    def test_paths_are_scoped_to_the_configured_session(self):
        self.assertTrue(hasattr(session, "build_session_paths"), "session-scoped paths are required")
        with tempfile.TemporaryDirectory() as root:
            paths = session.build_session_paths(
                env={"IE_SESSION_ID": "Agent 1 / GTA", "IE_SESSION_ROOT": root},
                pid=42,
            )

            self.assertEqual(paths.session_id, "Agent-1-GTA")
            self.assertEqual(paths.directory, Path(root) / "Agent-1-GTA")
            self.assertEqual(paths.log_file, paths.directory / "ie-mcp.log")
            self.assertEqual(paths.lock_file, paths.directory / "session.lock")
            self.assertEqual(paths.owner_file, paths.directory / "owner.json")

    def test_default_session_id_is_unique_to_the_mcp_process(self):
        self.assertTrue(hasattr(session, "build_session_paths"), "automatic process IDs are required")
        with tempfile.TemporaryDirectory() as root:
            paths = session.build_session_paths(env={"IE_SESSION_ROOT": root}, pid=314)

            self.assertEqual(paths.session_id, "pid-314")


class SessionLeaseTests(unittest.TestCase):
    def test_same_session_id_is_exclusive_but_different_sessions_are_parallel(self):
        self.assertTrue(hasattr(session, "SessionLease"), "atomic per-session leases are required")
        with tempfile.TemporaryDirectory() as root:
            first_paths = session.build_session_paths(
                env={"IE_SESSION_ID": "agent-a", "IE_SESSION_ROOT": root}, pid=101
            )
            second_paths = session.build_session_paths(
                env={"IE_SESSION_ID": "agent-b", "IE_SESSION_ROOT": root}, pid=202
            )
            first = session.SessionLease(first_paths, pid=101, pid_alive=lambda pid: pid == 101)
            duplicate = session.SessionLease(first_paths, pid=202, pid_alive=lambda pid: pid == 101)
            second = session.SessionLease(second_paths, pid=202, pid_alive=lambda pid: True)

            first.acquire()
            second.acquire()
            with self.assertRaisesRegex(RuntimeError, "already owned"):
                duplicate.acquire()

            first.release()
            second.release()

    def test_stale_session_lease_is_reclaimed_atomically(self):
        with tempfile.TemporaryDirectory() as root:
            paths = session.build_session_paths(
                env={"IE_SESSION_ID": "agent-a", "IE_SESSION_ROOT": root}, pid=202
            )
            paths.directory.mkdir(parents=True)
            paths.lock_file.write_text(
                json.dumps({"session_id": "agent-a", "owner_pid": 101}), encoding="utf-8"
            )
            lease = session.SessionLease(paths, pid=202, pid_alive=lambda _pid: False)

            lease.acquire()

            lock = json.loads(paths.lock_file.read_text(encoding="utf-8"))
            self.assertEqual(lock["owner_pid"], 202)
            lease.release()

    def test_lease_default_liveness_check_is_the_exact_pid_alive(self):
        lease = session.SessionLease(session.build_session_paths(
            env={"IE_SESSION_ROOT": tempfile.gettempdir()}, pid=1), pid=1)

        self.assertIs(lease.pid_alive, session.pid_alive)

    def test_takeover_keeps_an_owner_record_that_has_no_lock_file(self):
        # The holder died after deleting its lock but before its owner record: the new
        # lease must not inherit (or overwrite) the dead holder's process fields.
        with tempfile.TemporaryDirectory() as root:
            paths = session.build_session_paths(
                env={"IE_SESSION_ID": "s", "IE_SESSION_ROOT": root}, pid=222)
            paths.directory.mkdir(parents=True)
            paths.owner_file.write_text(json.dumps(
                {"session_id": "s", "owner_pid": 111, "driver_pid": 5,
                 "driver_identity": ["iedriverserver.exe", 5]}), encoding="utf-8")
            lease = session.SessionLease(paths, pid=222, pid_alive=lambda pid: pid == 222)

            lease.acquire()

            self.assertTrue((paths.directory / "owner.stale-111.json").exists())
            fresh = json.loads(paths.owner_file.read_text(encoding="utf-8"))
            self.assertEqual(fresh["owner_pid"], 222)
            self.assertIsNone(fresh.get("driver_identity"))
            lease.release()


class PidAliveTests(unittest.TestCase):
    def test_pid_alive_requires_exact_pid_match(self):
        row = '"python.exe","123","Console","1","10,000 K"\n'
        run = lambda *a, **k: SimpleNamespace(stdout=row, returncode=0)  # noqa: E731

        self.assertFalse(session.pid_alive(12, run=run))
        self.assertFalse(session.pid_alive(1234, run=run))
        self.assertTrue(session.pid_alive(123, run=run))

    def test_pid_alive_is_false_when_tasklist_lists_nothing(self):
        info = "INFO: No tasks are running which match the specified criteria.\n"
        run = lambda *a, **k: SimpleNamespace(stdout=info, returncode=0)  # noqa: E731

        self.assertFalse(session.pid_alive(123, run=run))


class KillVerifiedTests(unittest.TestCase):
    def test_kill_skips_reused_pid(self):
        calls = []
        run = lambda argv, **k: calls.append(argv) or SimpleNamespace(returncode=0)  # noqa: E731
        recorded = ("msedge.exe", 100)

        reused = session.kill_verified(
            [(7, recorded)], identity=lambda pid: ("notepad.exe", 200), run=run)
        self.assertEqual(reused, [])
        self.assertEqual(calls, [])

        same = session.kill_verified(
            [(7, recorded)], identity=lambda pid: ("msedge.exe", 100), run=run)
        self.assertEqual(same, [7])
        self.assertEqual(calls, [["taskkill", "/F", "/PID", "7"]])  # never /T

    def test_json_lists_compare_equal_to_identity_tuples(self):
        run = lambda argv, **k: SimpleNamespace(returncode=0)  # noqa: E731

        killed = session.kill_verified(
            [(7, ["msedge.exe", 100])], identity=lambda pid: ("msedge.exe", 100), run=run)

        self.assertEqual(killed, [7])

    def test_unknown_identity_never_killed(self):
        calls = []
        run = lambda argv, **k: calls.append(argv) or SimpleNamespace(returncode=0)  # noqa: E731

        killed = session.kill_verified(
            [(7, None)], identity=lambda pid: ("msedge.exe", 100), run=run)

        self.assertEqual(killed, [])
        self.assertEqual(calls, [])

    def test_a_process_that_is_gone_is_not_killed(self):
        calls = []
        run = lambda argv, **k: calls.append(argv) or SimpleNamespace(returncode=0)  # noqa: E731

        killed = session.kill_verified(
            [(7, ("msedge.exe", 100))], identity=lambda pid: None, run=run)

        self.assertEqual(killed, [])
        self.assertEqual(calls, [])

    def test_a_failed_taskkill_is_not_reported_as_killed(self):
        run = lambda argv, **k: SimpleNamespace(returncode=1)  # noqa: E731

        killed = session.kill_verified(
            [(7, ("msedge.exe", 100))], identity=lambda pid: ("msedge.exe", 100), run=run)

        self.assertEqual(killed, [])

    def test_malformed_recorded_identities_are_unknown_and_never_kill(self):
        calls = []
        run = lambda argv, **k: calls.append(argv) or SimpleNamespace(returncode=0)  # noqa: E731
        bad = [5, "msedge.exe", ["msedge.exe"], ["msedge.exe", "100"], ["msedge.exe", 1, 2],
               [100, "msedge.exe"], {"a": 1}, ["msedge.exe", True], "", 0.5]

        for recorded in bad:
            with self.subTest(recorded=recorded):
                killed = session.kill_verified(
                    [(7, recorded)], identity=lambda pid: ("msedge.exe", 100), run=run)

                self.assertEqual(killed, [])
        self.assertEqual(calls, [])

    def test_kill_pids_no_longer_exists(self):
        self.assertFalse(hasattr(session, "kill_pids"))


class ListBrowsersTests(unittest.TestCase):
    def _rows(self, output):
        run = lambda *a, **k: SimpleNamespace(stdout=output, returncode=0)  # noqa: E731
        with mock.patch.object(session.subprocess, "run", run):
            return session.list_ie_browsers()

    def test_rows_carry_the_parent_pid(self):
        rows = self._rows(
            "IEDriverServer.exe|5|900|C:\\d\\IEDriverServer.exe --port=1\n"
            "msedge.exe|6|5|msedge.exe --ie-mode-force --user-data-dir=C:\\t\\IEDriver-ab-12 http://127.0.0.1/\n"
            "msedge.exe|7|6|msedge.exe --type=renderer --ie-mode-force\n")

        self.assertEqual([(r["pid"], r["kind"], r["parent"]) for r in rows],
                         [(5, "iedriver", 900), (6, "edge-ie", 5)])
        self.assertEqual(rows[1]["profile"], "ab-12")
        self.assertEqual(rows[1]["url"], "http://127.0.0.1/")

    def test_a_line_without_the_parent_column_still_parses(self):
        rows = self._rows("msedge.exe|6|msedge.exe --ie-mode-force IEDriver-cd http://127.0.0.1/\n"
                          "msedge.exe|8||msedge.exe --ie-mode-force IEDriver-ef\n")

        self.assertEqual([(r["pid"], r["parent"], r["profile"]) for r in rows],
                         [(6, None, "cd"), (8, None, "ef")])


class ProcessIdentityTests(unittest.TestCase):
    def test_own_process_has_an_identity_and_an_impossible_pid_has_none(self):
        identity = processes.process_identity(os.getpid())

        self.assertIsNotNone(identity)
        name, created = identity
        self.assertTrue(name.endswith(".exe") and name == name.lower(), name)
        self.assertGreater(created, 0)
        self.assertEqual(processes.process_identity(os.getpid()), identity)
        self.assertIsNone(processes.process_identity(0x7FFFFFF0))


class CleanupTests(unittest.TestCase):
    def _write_owner(self, root, session_id, filename="owner.json", **values):
        directory = Path(root) / session_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / filename).write_text(
            json.dumps({"session_id": session_id, **values}), encoding="utf-8"
        )

    def test_cleanup_kills_only_processes_owned_by_stale_sessions(self):
        self.assertTrue(hasattr(session, "cleanup_stale_sessions"), "scoped cleanup is required")
        with tempfile.TemporaryDirectory() as root:
            self._write_owner(
                root,
                "live-agent",
                owner_pid=11,
                driver_pid=101,
                edge_pid=201,
                profile="live-profile",
                driver_identity=["iedriverserver.exe", 101],
                edge_identity=["msedge.exe", 201],
            )
            self._write_owner(
                root,
                "stale-agent",
                owner_pid=22,
                driver_pid=102,
                edge_pid=202,
                profile="stale-profile",
                driver_identity=["iedriverserver.exe", 102],
                edge_identity=["msedge.exe", 202],
            )
            browser_rows = [
                {"pid": 101, "kind": "iedriver", "profile": None},
                {"pid": 201, "kind": "edge-ie", "profile": "live-profile"},
                {"pid": 102, "kind": "iedriver", "profile": None},
                {"pid": 202, "kind": "edge-ie", "profile": "stale-profile"},
                {"pid": 999, "kind": "edge-ie", "profile": "untracked"},
            ]
            killed = []

            result = session.cleanup_stale_sessions(
                Path(root),
                browser_rows=browser_rows,
                pid_alive=lambda pid: pid == 11,
                identity=_recorded_identity,
                kill=lambda targets: killed.extend(_pids(targets)) or _pids(targets),
            )

            self.assertEqual(killed, [102, 202])
            self.assertEqual(result["killed"], [102, 202])
            self.assertEqual(result["skipped_live_sessions"], ["live-agent"])
            self.assertEqual(result["untracked_process_pids"], [101, 201, 999])

    def test_cleanup_preserves_reused_pids_when_the_edge_profile_does_not_match(self):
        with tempfile.TemporaryDirectory() as root:
            self._write_owner(
                root,
                "stale-agent",
                owner_pid=22,
                driver_pid=102,
                edge_pid=202,
                profile="old-profile",
                driver_identity=["iedriverserver.exe", 102],
                edge_identity=["msedge.exe", 202],
            )
            browser_rows = [
                {"pid": 102, "kind": "iedriver", "profile": None},
                {"pid": 202, "kind": "edge-ie", "profile": "new-agent-profile"},
            ]
            killed = []

            result = session.cleanup_stale_sessions(
                Path(root),
                browser_rows=browser_rows,
                pid_alive=lambda _pid: False,
                identity=_recorded_identity,
                kill=lambda targets: killed.extend(_pids(targets)) or _pids(targets),
            )

            self.assertEqual(killed, [])
            self.assertEqual(result["untracked_process_pids"], [102, 202])

    def test_cleanup_preserves_processes_whose_identity_changed(self):
        with tempfile.TemporaryDirectory() as root:
            self._write_owner(
                root, "stale-agent", owner_pid=22, driver_pid=102, edge_pid=202,
                profile="stale-profile",
                driver_identity=["iedriverserver.exe", 102],
                edge_identity=["msedge.exe", 999],  # recorded creation time differs
            )
            browser_rows = [
                {"pid": 102, "kind": "iedriver", "profile": None},
                {"pid": 202, "kind": "edge-ie", "profile": "stale-profile"},
            ]
            killed = []

            session.cleanup_stale_sessions(
                Path(root), browser_rows=browser_rows, pid_alive=lambda _pid: False,
                identity=_recorded_identity, kill=lambda targets: killed.extend(targets) or [])

            self.assertEqual(killed, [])

    def test_cleanup_ignores_a_malformed_identity_in_a_record(self):
        for bad in (5, "msedge.exe", ["msedge.exe", "x"], {"a": 1}):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as root:
                self._write_owner(
                    root, "stale-agent", owner_pid=22, driver_pid=102, edge_pid=202,
                    profile="stale-profile", driver_identity=bad, edge_identity=bad)
                browser_rows = [
                    {"pid": 102, "kind": "iedriver", "profile": None},
                    {"pid": 202, "kind": "edge-ie", "profile": "stale-profile"},
                ]
                killed = []

                session.cleanup_stale_sessions(
                    Path(root), browser_rows=browser_rows, pid_alive=lambda _pid: False,
                    identity=_recorded_identity, kill=lambda targets: killed.extend(targets) or [])

                self.assertEqual(killed, [])

    def test_cleanup_never_kills_a_record_without_identities(self):
        with tempfile.TemporaryDirectory() as root:
            self._write_owner(
                root, "stale-agent", owner_pid=22, driver_pid=102, edge_pid=202,
                profile="stale-profile")
            browser_rows = [
                {"pid": 102, "kind": "iedriver", "profile": None},
                {"pid": 202, "kind": "edge-ie", "profile": "stale-profile"},
            ]
            killed = []

            session.cleanup_stale_sessions(
                Path(root), browser_rows=browser_rows, pid_alive=lambda _pid: False,
                identity=_recorded_identity, kill=lambda targets: killed.extend(targets) or [])

            self.assertEqual(killed, [])

    def test_cleanup_reads_stale_takeover_records_and_clears_them(self):
        with tempfile.TemporaryDirectory() as root:
            self._write_owner(
                root, "s", filename="owner.stale-111.json", owner_pid=111, driver_pid=5,
                edge_pid=6, profile="p",
                driver_identity=["iedriverserver.exe", 5], edge_identity=["msedge.exe", 6])
            browser_rows = [{"pid": 6, "kind": "edge-ie", "profile": "p"},
                            {"pid": 5, "kind": "iedriver", "profile": None}]
            session.cleanup_stale_sessions(
                Path(root), browser_rows=browser_rows, pid_alive=lambda _pid: False,
                identity=_recorded_identity, kill=lambda targets: _pids(targets))

            record = json.loads(
                (Path(root) / "s" / "owner.stale-111.json").read_text(encoding="utf-8"))
            self.assertIsNone(record["driver_pid"])
            self.assertIsNone(record["edge_pid"])
            self.assertIsNone(record["driver_identity"])
            self.assertIsNone(record["edge_identity"])

    def test_stale_takeover_keeps_old_record_for_cleanup(self):
        with tempfile.TemporaryDirectory() as root:
            paths = session.build_session_paths(
                env={"IE_SESSION_ID": "s", "IE_SESSION_ROOT": root}, pid=222)
            paths.directory.mkdir(parents=True)
            old = {"session_id": "s", "owner_pid": 111, "driver_pid": 5, "edge_pid": 6,
                   "profile": "p", "driver_identity": ["iedriverserver.exe", 5],
                   "edge_identity": ["msedge.exe", 6]}
            paths.lock_file.write_text(
                json.dumps({"session_id": "s", "owner_pid": 111}), encoding="utf-8")
            paths.owner_file.write_text(json.dumps(old), encoding="utf-8")
            lease = session.SessionLease(paths, pid=222, pid_alive=lambda pid: pid == 222)

            lease.acquire()

            self.assertTrue((paths.directory / "owner.stale-111.json").exists())
            fresh = json.loads(paths.owner_file.read_text(encoding="utf-8"))
            self.assertEqual(fresh["owner_pid"], 222)
            self.assertIsNone(fresh.get("edge_identity"))
            calls = []
            result = session.cleanup_stale_sessions(
                Path(root),
                browser_rows=[{"pid": 6, "kind": "edge-ie", "profile": "p"},
                              {"pid": 5, "kind": "iedriver", "profile": None}],
                pid_alive=lambda pid: pid == 222,
                identity=_recorded_identity,
                kill=lambda targets: calls.append(_pids(targets)) or _pids(targets),
            )
            self.assertEqual(calls, [[5, 6]])
            self.assertEqual(result["killed"], [5, 6])
            self.assertEqual(result["skipped_live_sessions"], ["s"])
            lease.release()


if __name__ == "__main__":
    unittest.main()
