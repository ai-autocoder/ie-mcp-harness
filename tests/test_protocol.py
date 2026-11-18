import contextlib
import dataclasses
import io
import tempfile
import unittest

from ie_mcp import config, protocol
import ie_mcp.tools  # noqa: F401  (importing the package registers every tool)

UPSTREAM_TOOLS = {
    "ie_open", "ie_goto", "ie_status", "ie_browsers", "ie_kill_orphans", "ie_frames", "ie_text",
    "ie_html", "ie_click", "ie_fill", "ie_upload", "ie_dialog", "ie_js", "ie_screenshot",
    "ie_wait_text", "ie_wait_ready", "ie_select", "ie_press_key", "ie_back", "ie_forward",
    "ie_hover", "ie_resize", "ie_get", "ie_wait_element", "ie_scroll", "ie_grid", "ie_sleep",
    "ie_close",
}
EMPTY_SCHEMA = {"type": "object", "properties": {}}


def _call(name, arguments=None, mid=1):
    return {"jsonrpc": "2.0", "id": mid, "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}}}


class ProtocolTests(unittest.TestCase):
    def _temporary_tool(self, name, fn, **options):
        protocol.tool(name, "temporary test tool", EMPTY_SCHEMA, **options)(fn)
        self.addCleanup(protocol.REGISTRY.pop, name, None)
        return protocol.REGISTRY[name]

    def _isolated_settings(self, **changes):
        """Replace config.SETTINGS for one test; the server log goes to a temp dir."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        original = config.SETTINGS
        paths = config.build_session_paths(env={"IE_SESSION_ROOT": tmp.name}, pid=1)
        config.SETTINGS = dataclasses.replace(original, session_paths=paths, **changes)
        self.addCleanup(setattr, config, "SETTINGS", original)

    def test_tools_list_contains_all_upstream_tools(self):
        response = protocol.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

        names = {t["name"] for t in response["result"]["tools"]}
        self.assertEqual(names, UPSTREAM_TOOLS)
        for entry in response["result"]["tools"]:
            self.assertEqual(set(entry), {"name", "description", "inputSchema"})

    def test_unknown_tool_returns_jsonrpc_error(self):
        response = protocol.handle(_call("ie_no_such_tool"))

        self.assertEqual(response["error"]["code"], -32602)
        self.assertIn("ie_no_such_tool", response["error"]["message"])

    def test_tool_exception_becomes_is_error_result(self):
        def boom(args):
            raise RuntimeError("kaput")

        self._temporary_tool("ie_test_boom", boom, needs_driver=False)
        self._isolated_settings()

        with contextlib.redirect_stderr(io.StringIO()):
            response = protocol.handle(_call("ie_test_boom"))

        result = response["result"]
        self.assertIs(result["isError"], True)
        self.assertTrue(result["content"][0]["text"].startswith("ERROR in"))
        self.assertIn("kaput", result["content"][0]["text"])

    def test_initialize_returns_instructions(self):
        response = protocol.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )

        self.assertIn("instructions", response["result"])
        self.assertEqual(response["result"]["serverInfo"]["name"], "ie-mcp")

    def test_bool_needs_driver_becomes_callable(self):
        spec = self._temporary_tool("ie_test_nodriver", lambda args: protocol.text_result("x"),
                                    needs_driver=False)
        default = self._temporary_tool("ie_test_driver", lambda args: protocol.text_result("x"))

        self.assertIs(spec.needs_driver({}), False)
        self.assertIs(default.needs_driver({}), True)
        self.assertIsNone(spec.budget)

    def test_callable_needs_driver_and_budget_are_kept(self):
        spec = self._temporary_tool(
            "ie_test_callable",
            lambda args: protocol.text_result("x"),
            needs_driver=lambda args: bool(args.get("x")),
            budget=lambda args: 7.0,
        )

        self.assertIs(spec.needs_driver({"x": 1}), True)
        self.assertIs(spec.needs_driver({}), False)
        self.assertEqual(spec.budget({}), 7.0)

    def test_registration_order_and_duplicates(self):
        self._temporary_tool("ie_test_dup", lambda args: protocol.text_result("x"))

        with self.assertRaises(ValueError):
            protocol.tool("ie_test_dup", "again", EMPTY_SCHEMA)(lambda args: None)

    def test_settings_read_at_call_time(self):
        def read_timeout(args):
            return protocol.text_result(str(config.SETTINGS.page_timeout))

        self._temporary_tool("ie_test_settings", read_timeout, needs_driver=False)
        self._isolated_settings(page_timeout=99)

        response = protocol.handle(_call("ie_test_settings"))

        self.assertEqual(response["result"]["content"][0]["text"], "99")

    def test_serve_answers_one_line_per_request(self):
        stdin = io.StringIO(
            '{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
            "\n"
            "not json\n"
            '{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
        )
        stdout = io.StringIO()
        self._isolated_settings()

        with contextlib.redirect_stderr(io.StringIO()):
            protocol.serve(stdin, stdout)

        self.assertEqual(stdout.getvalue(), '{"jsonrpc": "2.0", "id": 1, "result": {}}\n')

    def test_image_result_is_base64_png(self):
        result = protocol.image_result(b"\x89PNG")

        self.assertEqual(result["content"][0]["type"], "image")
        self.assertEqual(result["content"][0]["mimeType"], "image/png")
        self.assertEqual(result["content"][0]["data"], "iVBORw==")


if __name__ == "__main__":
    unittest.main()
