import dataclasses
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ie_mcp import config, protocol
import ie_mcp.tools  # noqa: F401  (importing the package registers every tool)
from ie_mcp.session import SESSION


def _call(arguments):
    return protocol.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": "ie_click", "arguments": arguments}})


class FakeElement:
    def __init__(self):
        self.click_calls = 0

    def click(self):
        self.click_calls += 1


class FakeDriver:
    def __init__(self):
        self.scripts = []

    def execute_script(self, script, *args):
        self.scripts.append((script, args))
        return "queued"


class ClickModeTests(unittest.TestCase):
    def setUp(self):
        stderr = mock.patch.object(sys, "stderr", io.StringIO())
        stderr.start()
        self.addCleanup(stderr.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        original = config.SETTINGS
        paths = config.build_session_paths(env={"IE_SESSION_ROOT": tmp.name}, pid=1)
        config.SETTINGS = dataclasses.replace(original, session_paths=paths)
        self.addCleanup(setattr, config, "SETTINGS", original)
        self.element = FakeElement()
        self.driver = FakeDriver()
        for patcher in (mock.patch.object(SESSION, "ensure", return_value=self.driver),
                        mock.patch.object(SESSION, "find", return_value=self.element),
                        mock.patch("ie_mcp.tools.interact.time.sleep")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _click(self, **args):
        args = {"by": "id", "value": "go", **args}
        return _call(args)["result"]

    def test_async_click_uses_settimeout_script(self):
        result = self._click(mode="async")

        self.assertNotIn("isError", result)
        script, args = self.driver.scripts[0]
        self.assertIn("setTimeout", script)
        self.assertEqual(args, (self.element,))
        self.assertEqual(self.element.click_calls, 0)

    def test_driver_click_mode_calls_element_click(self):
        result = self._click(mode="driver")

        self.assertNotIn("isError", result)
        self.assertEqual(self.element.click_calls, 1)
        self.assertEqual(self.driver.scripts, [])

    def test_default_mode_comes_from_the_setting(self):
        config.SETTINGS = dataclasses.replace(config.SETTINGS, click_mode="async")
        self._click()
        self.assertEqual(self.element.click_calls, 0)
        self.assertIn("setTimeout", self.driver.scripts[0][0])

        config.SETTINGS = dataclasses.replace(config.SETTINGS, click_mode="driver")
        self._click()
        self.assertEqual(self.element.click_calls, 1)

    def test_explicit_mode_overrides_the_setting(self):
        config.SETTINGS = dataclasses.replace(config.SETTINGS, click_mode="async")

        self._click(mode="driver")

        self.assertEqual(self.element.click_calls, 1)

    def test_double_and_right_always_use_the_driver(self):
        for action, method in (("double", "double_click"), ("right", "context_click")):
            with self.subTest(action=action), \
                    mock.patch("selenium.webdriver.common.action_chains.ActionChains") as chains:
                result = self._click(mode="async", action=action)

                self.assertNotIn("isError", result)
                getattr(chains.return_value, method).assert_called_once_with(self.element)
                self.assertEqual(self.driver.scripts, [])

    def test_unknown_mode_is_an_error(self):
        result = self._click(mode="sideways")

        self.assertTrue(result["isError"])
        self.assertIn("driver|async", result["content"][0]["text"])
        self.assertEqual(self.element.click_calls, 0)

    def _hooked_click(self, **args):
        calls = []
        saved = protocol.POST_HOOKS
        protocol.POST_HOOKS = [lambda spec, a, result: calls.append(spec.name)]
        self.addCleanup(setattr, protocol, "POST_HOOKS", saved)
        self._click(**args)
        return calls

    def test_post_hooks_follow_the_effective_click_mode(self):
        config.SETTINGS = dataclasses.replace(config.SETTINGS, click_mode="async")
        self.assertEqual(self._hooked_click(), [], "IE_CLICK_MODE=async, no mode argument")

        config.SETTINGS = dataclasses.replace(config.SETTINGS, click_mode="driver")
        self.assertEqual(self._hooked_click(), ["ie_click"], "IE_CLICK_MODE=driver, no mode argument")

    def test_explicit_mode_decides_for_the_post_hooks(self):
        config.SETTINGS = dataclasses.replace(config.SETTINGS, click_mode="driver")
        self.assertEqual(self._hooked_click(mode="async"), [])

        config.SETTINGS = dataclasses.replace(config.SETTINGS, click_mode="async")
        self.assertEqual(self._hooked_click(mode="driver"), ["ie_click"])

    def test_double_click_in_async_mode_still_runs_the_post_hooks(self):
        config.SETTINGS = dataclasses.replace(config.SETTINGS, click_mode="async")
        with mock.patch("selenium.webdriver.common.action_chains.ActionChains"):
            self.assertEqual(self._hooked_click(action="double"), ["ie_click"])

    def test_description_warns_that_async_is_script_initiated(self):
        spec = protocol.REGISTRY["ie_click"]

        self.assertIn("script-initiated", spec.description)
        self.assertIn("window.open", spec.description)
        self.assertEqual(spec.input_schema["properties"]["mode"]["type"], "string")


class ClickModeSettingTests(unittest.TestCase):
    def _settings(self, env):
        return config.load_settings(env, home=Path("nowhere-at-all"))

    def test_default_is_driver_in_both_interaction_modes(self):
        # Spike D1: a driver click never blocked, and synthetic input (upstream's old
        # background mode) clicked nothing, so async is never the default.
        self.assertEqual(self._settings({}).click_mode, "driver")
        self.assertEqual(self._settings({"IE_INTERACTION_MODE": "native"}).click_mode, "driver")
        self.assertEqual(self._settings({"IE_INTERACTION_MODE": "background"}).click_mode, "driver")

    def test_env_var_selects_the_mode(self):
        self.assertEqual(self._settings({"IE_CLICK_MODE": "async"}).click_mode, "async")
        self.assertEqual(self._settings({"IE_CLICK_MODE": " Driver "}).click_mode, "driver")

    def test_invalid_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            self._settings({"IE_CLICK_MODE": "fast"})


if __name__ == "__main__":
    unittest.main()
