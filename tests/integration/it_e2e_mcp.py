"""The whole server over stdio: ``python -m ie_mcp`` as a subprocess, spoken to in JSON-RPC.

The other integration modules call ``protocol.handle`` in this process. This one runs the real
entry point, the way an MCP client does, and so also covers the start-up, the stdin/stdout
framing and the shutdown when the client goes away (a modal dialog may be open then).

Spike D11: the session's initial URL is not proxied, so the session starts on ``index.htm`` and
every page measured is reached by navigation afterwards.
"""
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from ie_mcp.session import _same_process, kill_verified
from ie_mcp.win32.processes import process_identity
from tests.integration.base import IntegrationTestCase

ROOT = Path(__file__).resolve().parents[2]
CALL_TIMEOUT = 240  # a first session start may retry three times (75 s each)
EXIT_TIMEOUT = 20
REQUIRED_TOOLS = {"ie_windows", "ie_network", "ie_console", "ie_tabs", "ie_close_window"}


class McpClient:
    """A minimal MCP client: one request at a time, newline-delimited JSON-RPC."""

    def __init__(self, env, stderr):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "ie_mcp"], cwd=str(ROOT), env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=stderr)
        self.lines = queue.Queue()
        self.next_id = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for raw in self.proc.stdout:
            self.lines.put(raw.decode("utf-8"))
        self.lines.put(None)

    def _send(self, message):
        self.proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def notify(self, method):
        self._send({"jsonrpc": "2.0", "method": method})

    def rpc(self, method, params=None):
        self.next_id += 1
        self._send({"jsonrpc": "2.0", "id": self.next_id, "method": method,
                    "params": params or {}})
        deadline = time.monotonic() + CALL_TIMEOUT
        while True:
            line = self.lines.get(timeout=max(deadline - time.monotonic(), 0.1))
            if line is None:
                raise AssertionError(f"the server closed stdout during {method}")
            response = json.loads(line)
            if response.get("id") == self.next_id:
                if "error" in response:
                    raise AssertionError(f"{method} failed: {response['error']}")
                return response["result"]

    def call(self, name, **arguments):
        return self.rpc("tools/call", {"name": name, "arguments": arguments})

    def close_stdin(self):
        self.proc.stdin.close()


class StdioServerTests(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        self._session_root = tempfile.TemporaryDirectory(prefix="ie-mcp-e2e-")
        self.addCleanup(self._session_root.cleanup)
        self.stderr = tempfile.TemporaryFile()
        self.addCleanup(self.stderr.close)
        self.client = None
        self.recorded = []

    # -- helpers -------------------------------------------------------------
    def start_server(self):
        env = dict(os.environ)
        env.pop("IE_MCP_INTEGRATION", None)
        env["IE_SESSION_ID"] = f"e2e-{os.getpid()}"
        env["IE_SESSION_ROOT"] = self._session_root.name
        self.client = McpClient(env, self.stderr)
        self.addCleanup(self._reap)
        init = self.client.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                              "clientInfo": {"name": "e2e", "version": "0"}})
        self.assertIn("serverInfo", init)
        self.client.notify("notifications/initialized")
        return self.client

    def _reap(self):
        """Whatever the test did: no server and none of its browser processes may be left."""
        proc = self.client.proc
        if proc.poll() is None:
            self.client.close_stdin()
            try:
                proc.wait(EXIT_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
        alive = [(pid, ident) for pid, ident in self.recorded if _same_process(pid, ident)]
        if alive:
            kill_verified(alive)
            self.stderr.seek(0)
            self.fail(f"browser processes survived the server: {[p for p, _ in alive]}\n"
                      + self.stderr.read().decode("utf-8", "replace")[-2000:])

    def texts_of(self, result):
        return [i["text"] for i in result["content"] if i.get("type") == "text"]

    def text(self, name, **arguments):
        result = self.client.call(name, **arguments)
        self.assertFalse(result.get("isError"), f"{name} failed: {self.texts_of(result)}")
        return self.texts_of(result)[0]

    def windows(self):
        return json.loads(self.text("ie_windows"))

    def dialog(self, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = [w for w in self.windows() if w["kind"] == "html_dialog"]
            if found:
                return found[0]
            time.sleep(0.25)
        return None

    def open_lookup_dialog(self):
        """Click the opener's button until its dialog shows (native input loses ~1 click in 6)."""
        for _ in range(4):
            self.client.call("ie_click", by="id", value="open_lookup", wait=0)
            dialog = self.dialog(timeout=6)
            if dialog:
                return dialog
        self.fail("open_lookup never opened a modal dialog")

    def note_server_processes(self):
        status = json.loads(self.text("ie_status"))
        for pid in (status["driver_pid"], status["edge_pid"]):
            ident = process_identity(pid) if pid else None
            if ident and (pid, ident) not in self.recorded:
                self.recorded.append((pid, ident))
        self.assertTrue(self.recorded, status)
        return status

    # -- tests ---------------------------------------------------------------
    def test_full_session_over_stdio(self):
        client = self.start_server()
        names = {t["name"] for t in client.rpc("tools/list")["tools"]}
        self.assertLessEqual(REQUIRED_TOOLS, names)

        # Frames: read a field of the "main" frame by id.
        self.text("ie_open", url=self.base_url + "index.htm", wait=1)
        self.text("ie_goto", url=self.base_url + "frameset.htm", wait=1)
        self.text("ie_wait_ready", timeout=30)
        self.assertEqual(self.text("ie_get", frame="main", by="id", value="code", attr="value"), "")
        status = self.note_server_processes()
        self.assertEqual(status["capture"], "proxy", status)

        # Fill and submit: the POST to /echo is in the network list.
        self.text("ie_fill", frame="main", by="id", value="code", text="E2E-1")
        def posted():
            # ie_network answers "no matching entries" (never an empty string) without a match.
            return self.text("ie_network", url_contains="/echo", method="POST")
        post = ""
        for _ in range(4):  # native input loses ~1 click in 6: click again when nothing was sent
            self.text("ie_click", frame="main", by="id", value="send", wait=1)
            deadline = time.monotonic() + 6
            while "/echo" not in post and time.monotonic() < deadline:
                post = posted()
                time.sleep(0.25)
            if "/echo" in post:
                break
        self.assertIn("/echo", post)
        self.assertEqual(self.text("ie_get", frame="main", by="id", value="echo_code"), "E2E-1")

        # A modal dialog: reached by its window handle (COM), picked from the grid.
        self.text("ie_goto", url=self.base_url + "modal_opener.htm", wait=1)
        dialog = self.open_lookup_dialog()
        self.text("ie_click", target=dialog["target"], by="id", value="pick_A1", wait=1)
        deadline = time.monotonic() + 10
        while self.dialog(timeout=0.1) and time.monotonic() < deadline:
            time.sleep(0.25)
        self.assertIsNone(self.dialog(timeout=0.1), "the dialog did not close")
        code = ""
        deadline = time.monotonic() + 10
        while code != "A1" and time.monotonic() < deadline:
            code = self.text("ie_get", by="id", value="lookup_code", attr="value")
            time.sleep(0.25)
        self.assertEqual(code, "A1")

        # No script error on any of these pages.
        records = json.loads(self.text("ie_console"))["records"]
        self.assertEqual([r for r in records if r["kind"] == "error"], [])

        # The screen as rendered.
        shot = self.client.call("ie_screenshot", scope="window")
        self.assertFalse(shot.get("isError"), self.texts_of(shot))
        self.assertGreaterEqual(len([i for i in shot["content"] if i.get("type") == "image"]), 1)

        self.text("ie_close")
        self.client.close_stdin()
        self.assertEqual(self.client.proc.wait(EXIT_TIMEOUT), 0)

    def test_server_exit_leaves_no_browser(self):
        client = self.start_server()
        self.text("ie_open", url=self.base_url + "modal_opener.htm", wait=1)
        self.open_lookup_dialog()
        self.note_server_processes()

        client.close_stdin()  # the client went away with the dialog still open

        client.proc.wait(EXIT_TIMEOUT)  # raises TimeoutExpired: the server did not exit
        left = [pid for pid, ident in self.recorded if _same_process(pid, ident)]
        self.assertEqual(left, [], "browser processes were left after the server exited")


if __name__ == "__main__":
    unittest.main()
