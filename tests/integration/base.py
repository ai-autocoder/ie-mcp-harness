"""Base class of the integration tests: they drive a real, visible IE-mode window.

Run them through ``python scripts/run_integration.py``: it sets ``IE_MCP_INTEGRATION=1`` and
fails on any skipped test. Without that variable every test here skips.

Every call goes through ``protocol.handle``, exactly as an MCP client's would. The fixture site
is served from ``IE_FIXTURE_HOST`` (default ``127.0.0.1``); its second origin is on
``localhost``, which IE puts in another zone (see ``ie_mcp_fixtures.server``).
"""
import contextlib
import io
import json
import os
import time
import unittest

import ie_mcp.tools  # noqa: F401  (importing the package registers every tool)
from ie_mcp import config, protocol
from ie_mcp.session import SESSION, _same_process, kill_verified
from ie_mcp_fixtures import start_fixture_server


def session_processes():
    """The (pid, identity) pairs of the session's IEDriver and Edge, as recorded right now."""
    return [(pid, identity) for pid, identity in
            ((SESSION.driver_pid, SESSION.driver_identity),
             (SESSION.edge_pid, SESSION.edge_identity)) if pid and identity]


def finish_session(recorded):
    """Close the session and make sure none of the processes it ever recorded is left.

    ``recorded`` holds every (pid, identity) pair the session had at any time during the
    tests, not only the final ones: ``ensure()`` can recreate the browser, and the old
    processes are then forgotten by ``SESSION``. Each step runs even if the one before it
    raised. A survivor is killed by its recorded identity (``kill_verified``: image name and
    creation time must still match) and reported as a failure, so a failed test never leaves
    a browser behind and a leak never goes unnoticed.
    """
    problems = []
    for step in (lambda: protocol.handle({"jsonrpc": "2.0", "id": 0, "method": "tools/call",
                                          "params": {"name": "ie_close", "arguments": {}}}),
                 SESSION.close):  # also releases the session lease: the next class starts clean
        try:
            step()
        except BaseException as exc:  # noqa: BLE001 - every step must run
            problems.append(repr(exc))
    alive = [(pid, identity) for pid, identity in recorded if _same_process(pid, identity)]
    if alive:
        kill_verified(alive)
        problems.append(f"processes of the session were still running (now killed): "
                        f"{[pid for pid, _ in alive]}")
    return problems


class IntegrationTestCase(unittest.TestCase):
    fixture = None
    base_url = None
    recorded_processes = None

    @classmethod
    def setUpClass(cls):
        if os.environ.get("IE_MCP_INTEGRATION") != "1":
            raise unittest.SkipTest("integration test: run it with scripts/run_integration.py")
        host = os.environ.get("IE_FIXTURE_HOST") or "127.0.0.1"
        cls.recorded_processes = []
        cls.fixture = start_fixture_server(host)
        cls.base_url = cls.fixture.base_url
        cls.alt_base_url = cls.fixture.alt_base_url

    @classmethod
    def remember_processes(cls):
        for pair in session_processes():
            if cls.recorded_processes is not None and pair not in cls.recorded_processes:
                cls.recorded_processes.append(pair)

    @classmethod
    def tearDownClass(cls):
        """Close the session, then insist that none of its processes is left."""
        cls.remember_processes()
        try:
            problems = finish_session(list(cls.recorded_processes or []))
        finally:
            if cls.fixture is not None:
                cls.fixture.stop()
                cls.fixture = None
        if problems:
            raise AssertionError("; ".join(problems))

    def setUp(self):
        self._saved_env = {}
        # The server's own log goes to stderr: keep the test output readable.
        stderr = contextlib.redirect_stderr(io.StringIO())
        stderr.__enter__()
        self.addCleanup(stderr.__exit__, None, None, None)
        self.addCleanup(self._restore_env)

    # -- calls ---------------------------------------------------------------
    def call(self, name, **arguments):
        """Call a tool as a client would; the MCP result dict (``content``, maybe ``isError``)."""
        response = protocol.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                    "params": {"name": name, "arguments": arguments}})
        self.remember_processes()  # the session may have been (re)created by this call
        self.assertIn("result", response, response)
        return response["result"]

    def texts(self, result):
        """Every text item of a result (NOTICE items included)."""
        return [item["text"] for item in result["content"] if item.get("type") == "text"]

    def text(self, name, **arguments):
        """The first text item of a successful call; fails the test on an error result."""
        result = self.call(name, **arguments)
        self.assertFalse(result.get("isError"), f"{name} failed: {self.texts(result)}")
        return self.texts(result)[0]

    def open_page(self, page, wait=1):
        """Open ``page`` of the fixture site (the first call starts the session on it)."""
        return self.text("ie_open", url=self.base_url + page, wait=wait)

    # -- windows -------------------------------------------------------------
    def eventually(self, check, timeout=10, interval=0.25, message="condition"):
        """Poll ``check()`` until it returns something truthy (returned), or fail after ``timeout``.

        Dialogs and queued (async/COM) clicks complete after the call that started them
        returned, so a test waits for the effect, never for the call.
        """
        deadline = time.monotonic() + timeout
        value = check()
        while not value:
            if time.monotonic() >= deadline:
                self.fail(f"timed out after {timeout}s waiting for: {message}")
            time.sleep(interval)
            value = check()
        return value

    def window_list(self):
        """``ie_windows`` as a list of dicts."""
        return json.loads(self.text("ie_windows"))

    def wait_window(self, kind, timeout=10, where=None):
        """The first listed window of ``kind`` (and ``where(window)`` true) within ``timeout`` s."""
        def find():
            return next((w for w in self.window_list()
                         if w["kind"] == kind and (where is None or where(w))), None)
        return self.eventually(find, timeout, message=f"a {kind} window in ie_windows")

    def wait_no_window(self, kind, timeout=10):
        self.eventually(lambda: not [w for w in self.window_list() if w["kind"] == kind],
                        timeout, message=f"no {kind} window left")

    def close_open_windows(self):
        """Close every dialog and pop-up the session has open (alerts first), then go back to
        the main window. Leaves a clean state for the next test; a system dialog is not touched
        (it would show up in ``ie_windows`` for the test to fail on)."""
        for _ in range(8):
            windows = self.window_list()
            order = {"script_dialog": 0, "html_dialog": 1, "popup": 2}
            victims = sorted((w for w in windows if w["kind"] in order),
                             key=lambda w: order[w["kind"]])
            if not victims:
                break
            for window in victims:
                self.call("ie_close_window", target=window["target"])
            time.sleep(0.5)
        self.call("ie_switch_window", main=True)

    # -- environment ---------------------------------------------------------
    def restart_session(self, **env):
        """Close the session, set ``env`` for the rest of this test and rebuild the settings.

        The previous values come back in ``tearDown``; the next ``ie_open`` starts a new
        browser with the new settings.
        """
        self.text("ie_close")
        for name, value in env.items():
            self._saved_env.setdefault(name, os.environ.get(name))
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = str(value)
        config.SETTINGS = config.load_settings(os.environ)

    def _restore_env(self):
        if not self._saved_env:
            return
        for name, value in self._saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self._saved_env = {}
        config.SETTINGS = config.load_settings(os.environ)
