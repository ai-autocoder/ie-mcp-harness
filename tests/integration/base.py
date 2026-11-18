"""Base class of the integration tests: they drive a real, visible IE-mode window.

Run them through ``python scripts/run_integration.py``: it sets ``IE_MCP_INTEGRATION=1`` and
fails on any skipped test. Without that variable every test here skips.

Every call goes through ``protocol.handle``, exactly as an MCP client's would. The fixture site
is served from ``IE_FIXTURE_HOST`` (default ``127.0.0.1``); its second origin is on
``localhost``, which IE puts in another zone (see ``ie_mcp_fixtures.server``).
"""
import contextlib
import io
import os
import unittest

import ie_mcp.tools  # noqa: F401  (importing the package registers every tool)
from ie_mcp import config, protocol
from ie_mcp.session import SESSION, _same_process
from ie_mcp_fixtures import start_fixture_server


class IntegrationTestCase(unittest.TestCase):
    fixture = None
    base_url = None

    @classmethod
    def setUpClass(cls):
        if os.environ.get("IE_MCP_INTEGRATION") != "1":
            raise unittest.SkipTest("integration test: run it with scripts/run_integration.py")
        host = os.environ.get("IE_FIXTURE_HOST") or "127.0.0.1"
        cls.fixture = start_fixture_server(host)
        cls.base_url = cls.fixture.base_url
        cls.alt_base_url = cls.fixture.alt_base_url

    @classmethod
    def tearDownClass(cls):
        """Close the session, then insist that none of its processes is left."""
        recorded = [(pid, identity) for pid, identity in
                    ((SESSION.driver_pid, SESSION.driver_identity),
                     (SESSION.edge_pid, SESSION.edge_identity)) if pid]
        try:
            protocol.handle({"jsonrpc": "2.0", "id": 0, "method": "tools/call",
                             "params": {"name": "ie_close", "arguments": {}}})
            SESSION.close()  # also releases the session lease: the next class starts clean
        finally:
            if cls.fixture is not None:
                cls.fixture.stop()
                cls.fixture = None
        alive = [pid for pid, identity in recorded if _same_process(pid, identity)]
        if alive:
            raise AssertionError(f"processes of the session are still running: {alive}")

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
