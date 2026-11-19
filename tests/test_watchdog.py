import contextlib
import dataclasses
import io
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from ie_mcp import config, protocol, session
import ie_mcp.tools  # noqa: F401  (importing the package registers every tool)
from ie_mcp.session import SESSION

EMPTY_SCHEMA = {"type": "object", "properties": {}}
WINDOW = {"kind": "html_dialog", "title": "Lookup", "hwnd": 42}


def _call(name, arguments=None, mid=1):
    return {"jsonrpc": "2.0", "id": mid, "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}}}


def _text(response):
    return response["result"]["content"][0]["text"]


def _ok(text="done"):
    return protocol.text_result(text)


class WatchdogTestCase(unittest.TestCase):
    """Fakes only: no browser. Every test leaves the module-level hooks and the lock as found."""

    def setUp(self):
        # The server log also goes to stderr; keep the test output pristine.
        stderr = mock.patch.object(sys, "stderr", io.StringIO())
        stderr.start()
        self.addCleanup(stderr.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        original = config.SETTINGS
        paths = config.build_session_paths(env={"IE_SESSION_ROOT": tmp.name}, pid=1)
        config.SETTINGS = dataclasses.replace(original, session_paths=paths, tool_timeout=0.5)
        self.addCleanup(setattr, config, "SETTINGS", original)
        for name in ("MODAL_PROBE", "POST_HOOKS", "AFTER_CALL", "CALL_CONTEXTS"):
            self.addCleanup(setattr, protocol, name, getattr(protocol, name))
        protocol.POST_HOOKS = []
        protocol.AFTER_CALL = []
        protocol.CALL_CONTEXTS = []
        protocol.MODAL_PROBE = lambda: []
        self.gates = []
        # Registered last, so it runs first: let deliberately stuck tools finish, then insist
        # that the driver lock is free again (a leak would hang every later test).
        self.addCleanup(self._drain)

    def _drain(self):
        for gate in self.gates:
            gate.set()
        self.assertTrue(session.DRIVER_LOCK.acquire(timeout=5), "DRIVER_LOCK was never released")
        session.DRIVER_LOCK.release()

    def gate(self):
        gate = threading.Event()
        self.gates.append(gate)
        return gate

    def tool(self, name, fn, **options):
        protocol.tool(name, "temporary test tool", EMPTY_SCHEMA, **options)(fn)
        self.addCleanup(protocol.REGISTRY.pop, name, None)
        return protocol.REGISTRY[name]

    def stuck_tool(self, name="ie_test_stuck", seconds=3, **options):
        """A tool that blocks until its gate opens (or ``seconds`` pass)."""
        gate = self.gate()
        started = threading.Event()

        def fn(args):
            started.set()
            gate.wait(seconds)
            return _ok("late")

        self.tool(name, fn, **options)
        return gate, started

    def call_in_thread(self, name, arguments=None):
        box = {}
        thread = threading.Thread(
            target=lambda: box.update(response=protocol.handle(_call(name, arguments))))
        thread.start()
        self.addCleanup(thread.join, 10)
        return box, thread


class WatchdogTests(WatchdogTestCase):
    def test_slow_tool_times_out_with_blocked_message(self):
        self.stuck_tool()

        started = time.monotonic()
        response = protocol.handle(_call("ie_test_stuck"))

        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(response["result"]["isError"])
        text = _text(response)
        self.assertTrue(text.startswith("BLOCKED:"), text)
        self.assertIn("may still complete", text)
        self.assertIn("ie_windows", text)
        self.assertIn("force=true", text)

    def test_declared_budget_extends_timeout(self):
        def fn(args):
            time.sleep(1)
            return _ok()

        self.tool("ie_test_budget", fn, budget=lambda args: 3)

        response = protocol.handle(_call("ie_test_budget"))

        self.assertNotIn("isError", response["result"])
        self.assertEqual(_text(response), "done")

    def test_call_budget_is_the_larger_of_setting_and_declared(self):
        spec = self.tool("ie_test_b", lambda a: _ok(), budget=lambda a: a["n"])
        plain = self.tool("ie_test_p", lambda a: _ok())

        self.assertEqual(protocol.call_budget(spec, {"n": 3}), 3)
        self.assertEqual(protocol.call_budget(spec, {"n": 0.1}), 0.5)
        self.assertEqual(protocol.call_budget(plain, {}), 0.5)

    def test_call_budget_survives_a_broken_budget_function(self):
        spec = self.tool("ie_test_bad_budget", lambda a: _ok(), budget=lambda a: a["missing"])

        self.assertEqual(protocol.call_budget(spec, {}), 0.5)

    def test_declared_budgets_follow_the_formulas(self):
        config.SETTINGS = dataclasses.replace(config.SETTINGS, tool_timeout=1, attach_retries=3)
        budget = lambda name, args=None: protocol.call_budget(protocol.REGISTRY[name], args or {})

        # ie_open/ie_goto also wait up to session.VERIFY_WAIT (5 s) for the proxy to show the
        # navigation (Task 13).
        self.assertEqual(budget("ie_open", {"wait": 4}), 3 * 75 + 4 + 15 + 5)
        self.assertEqual(budget("ie_open"), 3 * 75 + 4 + 15 + 5)
        self.assertEqual(budget("ie_goto", {"wait": 10}), 3 * 75 + 10 + 15 + 5)
        self.assertEqual(budget("ie_goto"), 3 * 75 + 3 + 15 + 5)
        self.assertEqual(budget("ie_wait_ready", {"timeout": 100, "interval": 5}), 120)
        self.assertEqual(budget("ie_wait_text", {"timeout": 100, "interval": 5}), 120)
        self.assertEqual(budget("ie_wait_element", {"timeout": 100, "interval": 5}), 120)
        self.assertEqual(budget("ie_wait_ready"), 60 + 2 + 15)
        self.assertEqual(budget("ie_wait_text"), 45 + 2 + 15)
        self.assertEqual(budget("ie_wait_element"), 20 + 1 + 15)
        self.assertEqual(budget("ie_click", {"timeout": 30, "wait": 5}), 50)
        self.assertEqual(budget("ie_click"), 10 + 2 + 15)
        self.assertEqual(budget("ie_fill", {"timeout": 30}), 45)
        self.assertEqual(budget("ie_sleep", {"seconds": 60}), 65)

    def test_budgets_fall_back_to_defaults_on_bad_arguments(self):
        config.SETTINGS = dataclasses.replace(config.SETTINGS, tool_timeout=1)

        for args in ({"timeout": "soon", "wait": None}, {"timeout": float("nan")},
                     {"timeout": -5}, {"timeout": True}):
            self.assertEqual(protocol.call_budget(protocol.REGISTRY["ie_click"], args), 27, args)

    def test_huge_budget_cannot_overflow_the_lock_timeout(self):
        spec = self.tool("ie_test_huge", lambda a: _ok(), budget=lambda a: 1e30)

        self.assertLessEqual(protocol.call_budget(spec, {}), threading.TIMEOUT_MAX)

    def test_busy_when_lock_held_by_running_tool(self):
        config.SETTINGS = dataclasses.replace(config.SETTINGS, tool_timeout=0.3)
        gate, started = self.stuck_tool()
        self.tool("ie_test_second", lambda args: _ok(), budget=lambda args: 0.3)
        self.call_in_thread("ie_test_stuck")
        self.assertTrue(started.wait(2))

        response = protocol.handle(_call("ie_test_second"))

        self.assertTrue(response["result"]["isError"])
        text = _text(response)
        self.assertTrue(text.startswith("BUSY:"), text)
        self.assertIn("ie_test_stuck", text)
        self.assertRegex(text, r"held the browser for \d+s")

    def test_busy_becomes_blocked_when_a_modal_is_open(self):
        config.SETTINGS = dataclasses.replace(config.SETTINGS, tool_timeout=0.3)
        gate, started = self.stuck_tool()
        self.tool("ie_test_second", lambda args: _ok())
        self.call_in_thread("ie_test_stuck")
        self.assertTrue(started.wait(2))
        protocol.MODAL_PROBE = lambda: [WINDOW]

        response = protocol.handle(_call("ie_test_second"))

        self.assertTrue(response["result"]["isError"])
        self.assertTrue(_text(response).startswith("BLOCKED: modal dialog open"), _text(response))
        self.assertIn("hwnd 42", response["result"]["content"][-1]["text"])

    def test_non_driver_tool_runs_while_driver_tool_is_stuck(self):
        gate, started = self.stuck_tool(seconds=10)
        self.tool("ie_test_free", lambda args: _ok("free"), needs_driver=False)
        self.call_in_thread("ie_test_stuck")
        self.assertTrue(started.wait(2))

        began = time.monotonic()
        response = protocol.handle(_call("ie_test_free"))

        self.assertLess(time.monotonic() - began, 2)
        self.assertEqual(_text(response), "free")

    def test_force_close_works_while_driver_locked(self):
        gate, started = self.stuck_tool()
        self.call_in_thread("ie_test_stuck")
        self.assertTrue(started.wait(2))
        self.addCleanup(SESSION.cancel.clear)

        with mock.patch.object(SESSION, "kill_own_processes", return_value=[]) as kill, \
                mock.patch.object(SESSION, "_forget_processes"):
            began = time.monotonic()
            response = protocol.handle(_call("ie_close", {"force": True}))

        self.assertLess(time.monotonic() - began, 1)
        self.assertNotIn("isError", response["result"])
        kill.assert_called_once_with()

    def test_lock_released_after_stuck_tool_finishes(self):
        gate, started = self.stuck_tool()
        response = protocol.handle(_call("ie_test_stuck"))
        self.assertTrue(_text(response).startswith("BLOCKED:"))
        self.assertTrue(session.DRIVER_LOCK.locked())

        gate.set()

        self.assertTrue(session.DRIVER_LOCK.acquire(timeout=2))
        session.DRIVER_LOCK.release()

    def test_late_result_of_a_blocked_call_is_discarded(self):
        gate, started = self.stuck_tool()
        calls = []
        protocol.POST_HOOKS = [lambda spec, args, result: calls.append("post")]
        protocol.AFTER_CALL = [lambda spec, args, result: calls.append("after")]
        response = protocol.handle(_call("ie_test_stuck"))
        self.assertTrue(_text(response).startswith("BLOCKED:"))

        gate.set()
        self.assertTrue(session.DRIVER_LOCK.acquire(timeout=2))
        session.DRIVER_LOCK.release()
        time.sleep(0.1)

        self.assertEqual(calls, [])

    def test_lock_released_after_tool_error(self):
        def boom(args):
            raise RuntimeError("kaput")

        self.tool("ie_test_boom", boom)

        response = protocol.handle(_call("ie_test_boom"))

        self.assertTrue(response["result"]["isError"])
        self.assertTrue(_text(response).startswith("ERROR in ie_test_boom: kaput"))
        self.assertFalse(session.DRIVER_LOCK.locked())

    def test_lock_released_after_a_normal_call(self):
        self.tool("ie_test_fine", lambda args: _ok())

        protocol.handle(_call("ie_test_fine"))

        self.assertFalse(session.DRIVER_LOCK.locked())

    def test_handle_goes_through_watchdog(self):
        config.SETTINGS = dataclasses.replace(config.SETTINGS, tool_timeout=0.3)
        self.stuck_tool()

        began = time.monotonic()
        response = protocol.handle(_call("ie_test_stuck"))

        self.assertLess(time.monotonic() - began, 1)
        self.assertTrue(_text(response).startswith("BLOCKED:"))

    def test_responses_carry_request_ids_when_out_of_order(self):
        # A generous budget: the slow tool must succeed even on a loaded machine.
        config.SETTINGS = dataclasses.replace(config.SETTINGS, tool_timeout=10)
        gate = self.gate()
        self.tool("ie_test_slow", lambda args: (gate.wait(8), _ok("slow"))[1], needs_driver=False)
        self.tool("ie_test_fast", lambda args: _ok("fast"), needs_driver=False)
        timer = threading.Timer(0.6, gate.set)
        self.addCleanup(timer.cancel)
        timer.start()
        stdin = io.StringIO(json.dumps(_call("ie_test_slow", mid=1)) + "\n"
                            + json.dumps(_call("ie_test_fast", mid=2)) + "\n")
        stdout = io.StringIO()

        protocol.serve(stdin, stdout)

        lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual([line["id"] for line in lines], [2, 1])
        self.assertEqual(lines[0]["result"]["content"][0]["text"], "fast")
        self.assertEqual(lines[1]["result"]["content"][0]["text"], "slow")

    def test_serve_waits_for_running_calls_at_end_of_input(self):
        self.tool("ie_test_slowish", lambda args: (time.sleep(0.3), _ok("finished"))[1])
        stdout = io.StringIO()

        protocol.serve(io.StringIO(json.dumps(_call("ie_test_slowish")) + "\n"), stdout)

        self.assertIn("finished", stdout.getvalue())

    def test_serve_keeps_answering_other_methods_inline(self):
        stdout = io.StringIO()

        protocol.serve(io.StringIO('{"jsonrpc":"2.0","id":7,"method":"ping"}\n'
                                   "not json\n[1,2]\n"), stdout)

        self.assertEqual(json.loads(stdout.getvalue()), {"jsonrpc": "2.0", "id": 7, "result": {}})

    def test_call_contexts_wrap_the_whole_call(self):
        events = []

        @contextlib.contextmanager
        def context():
            events.append(("enter", threading.get_ident()))
            try:
                yield
            finally:
                events.append(("exit", threading.get_ident()))

        def fn(args):
            events.append(("fn", threading.get_ident()))
            return _ok()

        self.tool("ie_test_ctx", fn)
        protocol.CALL_CONTEXTS = [context]
        protocol.POST_HOOKS = [lambda spec, args, result: events.append(("hook", threading.get_ident()))]
        protocol.AFTER_CALL = [lambda spec, args, result: events.append(("after", threading.get_ident()))]

        protocol.handle(_call("ie_test_ctx"))

        worker = next(ident for kind, ident in events if kind == "fn")
        self.assertEqual([kind for kind, ident in events if ident == worker], ["enter", "fn", "exit"])
        self.assertLess(events.index(("hook", next(i for k, i in events if k == "hook"))),
                        events.index(("exit", worker)))
        self.assertLess([k for k, i in events].index("after"),
                        events.index(("exit", worker)))

    def test_hook_threads_enter_call_contexts(self):
        entered = []

        @contextlib.contextmanager
        def context():
            entered.append(threading.current_thread().name)
            yield

        seen = {}
        self.tool("ie_test_hookctx", lambda args: _ok())
        protocol.CALL_CONTEXTS = [context]
        protocol.POST_HOOKS = [lambda s, a, r: seen.update(post=threading.current_thread().name)]
        protocol.AFTER_CALL = [lambda s, a, r: seen.update(after=threading.current_thread().name)]

        protocol.handle(_call("ie_test_hookctx"))

        # one entry per thread that ran something: the worker, the POST_HOOKS thread and the
        # AFTER_CALL thread, all different threads, none sharing a COM object.
        self.assertEqual(len(entered), 3)
        self.assertEqual(len(set(entered)), 3)
        self.assertIn(seen["post"], entered)
        self.assertIn(seen["after"], entered)
        self.assertNotEqual(seen["post"], seen["after"])

    def test_notice_appended_when_modal_open(self):
        self.tool("ie_test_modal", lambda args: _ok("clicked"))
        protocol.MODAL_PROBE = lambda: [WINDOW]

        response = protocol.handle(_call("ie_test_modal"))

        content = response["result"]["content"]
        self.assertEqual(content[0]["text"], "clicked")
        self.assertEqual(
            content[-1]["text"],
            'NOTICE: html_dialog open: Lookup (hwnd 42). Act in it with ie_switch_window or '
            'target="hwnd:42"; ie_windows lists every window; ie_dialog answers a script '
            'alert or confirm.')

    def test_notice_has_one_item_per_window_and_follows_errors_and_blocks(self):
        def boom(args):
            raise RuntimeError("kaput")

        self.tool("ie_test_boom", boom)
        self.stuck_tool()
        protocol.MODAL_PROBE = lambda: [WINDOW, {"kind": "alert", "title": "Hi", "hwnd": 7}]

        errored = protocol.handle(_call("ie_test_boom"))["result"]
        blocked = protocol.handle(_call("ie_test_stuck"))["result"]

        for result in (errored, blocked):
            self.assertTrue(result["isError"])
            self.assertEqual([c["text"].split(":")[0] for c in result["content"]][-2:],
                             ["NOTICE", "NOTICE"])
            self.assertIn("hwnd 7", result["content"][-1]["text"])

    def test_probe_failure_is_treated_as_no_window(self):
        def probe():
            raise OSError("no desktop")

        self.tool("ie_test_probe", lambda args: _ok("fine"))
        protocol.MODAL_PROBE = probe

        response = protocol.handle(_call("ie_test_probe"))

        self.assertEqual(response["result"]["content"], [{"type": "text", "text": "fine"}])

    def test_post_hooks_skipped_when_modal_open_or_async(self):
        calls = []
        self.tool("ie_test_hooked", lambda args: _ok())
        protocol.POST_HOOKS = [lambda spec, args, result: calls.append(args)]

        protocol.handle(_call("ie_test_hooked"))
        self.assertEqual(len(calls), 1, "control: the hook runs normally")

        protocol.MODAL_PROBE = lambda: [WINDOW]
        protocol.handle(_call("ie_test_hooked"))
        protocol.MODAL_PROBE = lambda: []
        protocol.handle(_call("ie_test_hooked", {"mode": "async"}))

        self.assertEqual(len(calls), 1)

    def test_post_hooks_skipped_for_non_driver_tools_and_errors(self):
        calls = []

        def boom(args):
            raise RuntimeError("kaput")

        self.tool("ie_test_free", lambda args: _ok(), needs_driver=False)
        self.tool("ie_test_boom", boom)
        protocol.POST_HOOKS = [lambda spec, args, result: calls.append(spec.name)]

        protocol.handle(_call("ie_test_free"))
        protocol.handle(_call("ie_test_boom"))

        self.assertEqual(calls, [])

    def test_slow_hook_keeps_lock_but_returns_result(self):
        gate = self.gate()
        self.tool("ie_test_slowhook", lambda args: _ok("result"))
        protocol.POST_HOOKS = [lambda spec, args, result: gate.wait(8)]

        began = time.monotonic()
        response = protocol.handle(_call("ie_test_slowhook"))
        elapsed = time.monotonic() - began

        self.assertEqual(_text(response), "result")
        # About 3 s (POST_HOOK_WAIT); the hook itself would take 8 s.
        self.assertGreater(elapsed, 2.8)
        self.assertLess(elapsed, 6)
        self.assertTrue(session.DRIVER_LOCK.locked())
        gate.set()
        self.assertTrue(session.DRIVER_LOCK.acquire(timeout=2))
        session.DRIVER_LOCK.release()

    def test_post_hook_failure_does_not_change_result(self):
        def hook(spec, args, result):
            raise RuntimeError("hook broke")

        self.tool("ie_test_hookfail", lambda args: _ok("original"))
        protocol.POST_HOOKS = [hook]

        response = protocol.handle(_call("ie_test_hookfail"))

        self.assertEqual(response["result"], _ok("original"))
        self.assertTrue(session.DRIVER_LOCK.acquire(timeout=2), "hook failure leaked the lock")
        session.DRIVER_LOCK.release()

    def test_after_call_hooks_run_without_lock(self):
        seen = []
        self.tool("ie_test_after_driver", lambda args: _ok())
        self.tool("ie_test_after_free", lambda args: _ok(), needs_driver=False)
        protocol.AFTER_CALL = [lambda spec, args, result: seen.append(
            (spec.name, session.DRIVER_LOCK.locked()))]

        protocol.handle(_call("ie_test_after_driver"))
        protocol.handle(_call("ie_test_after_free"))

        self.assertEqual(seen, [("ie_test_after_driver", False), ("ie_test_after_free", False)])

    def test_slow_after_call_hook_is_not_waited_for_longer_than_two_seconds(self):
        gate = self.gate()
        self.tool("ie_test_after_slow", lambda args: _ok("result"), needs_driver=False)
        protocol.AFTER_CALL = [lambda spec, args, result: gate.wait(8)]

        began = time.monotonic()
        response = protocol.handle(_call("ie_test_after_slow"))
        elapsed = time.monotonic() - began

        self.assertEqual(_text(response), "result")
        # About 2 s (AFTER_CALL_WAIT); the hook itself would take 8 s.
        self.assertGreater(elapsed, 1.8)
        self.assertLess(elapsed, 5)

    def test_after_call_failure_does_not_change_result(self):
        def hook(spec, args, result):
            raise RuntimeError("hook broke")

        self.tool("ie_test_after_fail", lambda args: _ok("original"), needs_driver=False)
        protocol.AFTER_CALL = [hook]

        self.assertEqual(protocol.handle(_call("ie_test_after_fail"))["result"], _ok("original"))

    def test_status_does_not_wait_for_busy_driver(self):
        class FakeDriver:
            title = "Fixture"
            current_url = "http://127.0.0.1/"
            window_handles = ["a"]

        self.assertTrue(session.DRIVER_LOCK.acquire(timeout=1))
        self.addCleanup(lambda: session.DRIVER_LOCK.locked() and session.DRIVER_LOCK.release())
        with mock.patch.object(SESSION, "driver", FakeDriver()):
            began = time.monotonic()
            busy = protocol.handle(_call("ie_status"))
            elapsed = time.monotonic() - began
            session.DRIVER_LOCK.release()
            idle = protocol.handle(_call("ie_status"))

        self.assertLess(elapsed, 3)
        busy_info = json.loads(_text(busy))
        self.assertEqual(busy_info["driver"], "busy")
        self.assertNotIn("title", busy_info)
        idle_info = json.loads(_text(idle))
        self.assertEqual(idle_info["title"], "Fixture")
        self.assertNotIn("driver", idle_info)
        self.assertFalse(session.DRIVER_LOCK.locked(), "ie_status must release the lock")

    def test_busy_names_ie_status_while_it_holds_the_lock(self):
        config.SETTINGS = dataclasses.replace(config.SETTINGS, tool_timeout=0.3)
        gate = self.gate()
        reading = threading.Event()

        class SlowDriver:
            @property
            def title(self):
                reading.set()
                gate.wait(5)
                return "late"

            current_url = "http://127.0.0.1/"
            window_handles = []

        self.tool("ie_test_second", lambda args: _ok())
        with mock.patch.object(SESSION, "driver", SlowDriver()):
            self.call_in_thread("ie_status")
            self.assertTrue(reading.wait(2))

            response = protocol.handle(_call("ie_test_second"))
            gate.set()
            self.assertTrue(session.DRIVER_LOCK.acquire(timeout=3))
            session.DRIVER_LOCK.release()

        self.assertTrue(_text(response).startswith("BUSY:"), _text(response))
        self.assertIn("ie_status", _text(response))

    def test_base_exception_from_a_tool_becomes_an_error_result(self):
        def leaves(args):
            raise SystemExit(3)

        self.tool("ie_test_exit", leaves)
        protocol.POST_HOOKS = [lambda spec, args, result: self.fail("no hooks after a failure")]

        response = protocol.handle(_call("ie_test_exit"))

        self.assertTrue(response["result"]["isError"])
        self.assertTrue(_text(response).startswith("ERROR in ie_test_exit:"), _text(response))
        self.assertFalse(session.DRIVER_LOCK.locked())

    def test_base_exception_from_a_call_context_becomes_an_error_result(self):
        @contextlib.contextmanager
        def broken():
            raise SystemExit(4)
            yield

        self.tool("ie_test_ctxexit", lambda args: _ok())
        protocol.CALL_CONTEXTS = [broken]

        response = protocol.handle(_call("ie_test_ctxexit"))

        self.assertTrue(response["result"]["isError"])
        self.assertFalse(session.DRIVER_LOCK.locked())

    def test_defers_effect_predicate_replaces_the_mode_check(self):
        calls = []
        self.tool("ie_test_deferred", lambda args: _ok(), defers_effect=lambda a: a.get("queued"))
        protocol.POST_HOOKS = [lambda spec, args, result: calls.append(args)]

        protocol.handle(_call("ie_test_deferred", {"queued": True}))
        self.assertEqual(calls, [])
        protocol.handle(_call("ie_test_deferred", {"mode": "async"}))
        self.assertEqual(len(calls), 1, "the predicate, not args['mode'], decides")

    def test_failing_defers_effect_predicate_skips_the_hooks(self):
        calls = []
        self.tool("ie_test_deferred_bad", lambda args: _ok(), defers_effect=lambda a: a["missing"])
        protocol.POST_HOOKS = [lambda spec, args, result: calls.append(args)]

        response = protocol.handle(_call("ie_test_deferred_bad"))

        self.assertEqual(_text(response), "done")
        self.assertEqual(calls, [])

    def test_slow_tools_declare_what_they_need(self):
        for name in ("ie_sleep", "ie_browsers", "ie_kill_orphans", "ie_status"):
            self.assertFalse(protocol.REGISTRY[name].needs_driver({}), name)
        self.assertTrue(protocol.REGISTRY["ie_click"].needs_driver({}))
        self.assertTrue(protocol.REGISTRY["ie_close"].needs_driver({}))
        self.assertFalse(protocol.REGISTRY["ie_close"].needs_driver({"force": True}))


class ToolTimeoutSettingTests(unittest.TestCase):
    def _settings(self, env):
        return config.load_settings(env, home=Path("nowhere-at-all"))

    def test_default_is_45_seconds(self):
        self.assertEqual(self._settings({}).tool_timeout, 45)

    def test_env_var_sets_the_timeout(self):
        self.assertEqual(self._settings({"IE_TOOL_TIMEOUT": "12.5"}).tool_timeout, 12.5)

    def test_invalid_values_are_rejected(self):
        for value in ("soon", "0", "-3", "nan", "inf", ""):
            with self.subTest(value=value):
                if value == "":  # an empty variable means "unset"
                    self.assertEqual(self._settings({"IE_TOOL_TIMEOUT": value}).tool_timeout, 45)
                else:
                    with self.assertRaises(ValueError):
                        self._settings({"IE_TOOL_TIMEOUT": value})


if __name__ == "__main__":
    unittest.main()
