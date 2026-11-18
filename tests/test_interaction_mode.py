import unittest

from ie_mcp import config, protocol


class InteractionModeTests(unittest.TestCase):
    def test_native_mode_preserves_focused_os_events(self):
        mode = config.resolve_interaction_mode({"IE_INTERACTION_MODE": "native"})

        self.assertEqual(mode.name, "native")
        self.assertTrue(mode.require_window_focus)
        self.assertTrue(mode.native_events)

    def test_background_mode_sends_native_events_without_window_focus(self):
        # Spike D17: synthetic events (native_events=False) do nothing on the development
        # machine, so background means native input without requiring window focus.
        mode = config.resolve_interaction_mode({"IE_INTERACTION_MODE": "background"})

        self.assertEqual(mode.name, "background")
        self.assertFalse(mode.require_window_focus)
        self.assertTrue(mode.native_events)

    def test_background_is_default_interaction_mode(self):
        self.assertEqual(config.resolve_interaction_mode({}).name, "background")

    def test_unknown_mode_fails_with_actionable_message(self):
        with self.assertRaisesRegex(ValueError, "native or background"):
            config.resolve_interaction_mode({"IE_INTERACTION_MODE": "headless"})

    def test_mcp_instructions_explain_when_agents_should_use_each_mode(self):
        response = protocol.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        instructions = response["result"]["instructions"]

        self.assertIn("IE_INTERACTION_MODE=background", instructions)
        self.assertIn("IE_INTERACTION_MODE=native", instructions)
        self.assertIn("active desktop", instructions)
        self.assertIn("fallback", instructions)

    def test_instructions_describe_background_default(self):
        response = protocol.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        instructions = response["result"]["instructions"]
        sentence = next(s for s in instructions.split(".") if "IE_INTERACTION_MODE=background" in s)

        self.assertIn("default", sentence)
        self.assertNotIn("synthetic", instructions)


if __name__ == "__main__":
    unittest.main()
