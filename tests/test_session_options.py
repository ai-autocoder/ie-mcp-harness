import dataclasses
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from ie_mcp import config, session


# Hermetic: never scan the real ~/.cache/selenium. This home does not exist.
_NO_HOME = Path(tempfile.gettempdir()) / "ie-mcp-tests-no-such-home" / uuid.uuid4().hex


def _settings(env):
    return config.load_settings(env, home=_NO_HOME)


class BuildOptionsTests(unittest.TestCase):
    def test_build_options_ignores_unhandled_prompts(self):
        options = session.build_options(_settings({}), None)

        self.assertEqual(options.unhandled_prompt_behavior, "ignore")

    def test_clean_session_is_opt_in(self):
        self.assertFalse(session.build_options(_settings({}), None).ensure_clean_session)
        on = session.build_options(_settings({"IE_CLEAN_SESSION": "1"}), None)
        self.assertTrue(on.ensure_clean_session)

    def test_background_mode_is_native_events_without_focus(self):
        options = session.build_options(_settings({}), None)

        self.assertTrue(options.native_events)
        self.assertFalse(options.require_window_focus)

    def test_native_mode_requires_focus(self):
        options = session.build_options(
            _settings({"IE_INTERACTION_MODE": "native"}), None)

        self.assertTrue(options.native_events)
        self.assertTrue(options.require_window_focus)

    def test_upstream_options_are_kept(self):
        settings = _settings({"IE_EDGE_PATH": "edge.exe"})

        options = session.build_options(settings, "http://127.0.0.1:1/")

        self.assertTrue(options.attach_to_edge_chrome)
        self.assertEqual(options.edge_executable_path, "edge.exe")
        self.assertEqual(options.page_load_strategy, "none")
        self.assertEqual(options.initial_browser_url, "http://127.0.0.1:1/")

    def test_no_proxy_capability_without_a_proxy_port(self):
        options = session.build_options(_settings({}), None)

        self.assertIsNone(options.to_capabilities().get("proxy"))
        self.assertFalse(options.use_per_process_proxy)

    def test_proxy_always_per_process(self):
        options = session.build_options(
            _settings({}), None, proxy_port=8123, bypass=["*.example.test"])

        self.assertTrue(options.use_per_process_proxy)
        self.assertEqual(options.proxy.noProxy, ["<-loopback>", "*.example.test"])
        self.assertEqual(options.proxy.httpProxy, "127.0.0.1:8123")
        self.assertEqual(options.proxy.sslProxy, "127.0.0.1:8123")
        self.assertEqual(options.to_capabilities()["proxy"]["noProxy"],
                         ["<-loopback>", "*.example.test"])

    def test_proxy_without_bypass_still_exempts_loopback(self):
        options = session.build_options(_settings({}), None, proxy_port=8123)

        self.assertEqual(options.proxy.noProxy, ["<-loopback>"])


class DriverEnvironmentTests(unittest.TestCase):
    def test_compat_layer_is_removed_case_insensitively(self):
        # Quirk (spike D16): the Claude desktop app sets __COMPAT_LAYER; inherited, it makes
        # Edge relaunch itself and IEDriver never finds the IE window.
        source = {"PATH": "p", "__COMPAT_LAYER": "DetectorsAppHealth", "__compat_layer": "x",
                  "Keep": "1"}

        env = session.driver_environment(source)

        self.assertEqual(env, {"PATH": "p", "Keep": "1"})

    def test_other_keys_are_kept_and_the_source_is_not_mutated(self):
        source = {"A": "1", "__COMPAT_LAYER": "X"}

        env = session.driver_environment(source)

        self.assertIsNot(env, source)
        self.assertIn("__COMPAT_LAYER", source)
        self.assertEqual(env["A"], "1")

    def test_defaults_to_the_process_environment_without_mutating_it(self):
        with mock.patch.dict(os.environ, {"__COMPAT_LAYER": "X"}):
            env = session.driver_environment()

            self.assertNotIn("__COMPAT_LAYER", {k.upper() for k in env})
            self.assertEqual(os.environ["__COMPAT_LAYER"], "X")
            self.assertEqual(env.get("PATH"), os.environ.get("PATH"))


class DriverTimeoutTests(unittest.TestCase):
    def test_timeout_constant_is_finite_300(self):
        self.assertEqual(session.DRIVER_HTTP_TIMEOUT, 300)

    def test_apply_driver_timeout_sets_the_real_selenium_client_config(self):
        from selenium.webdriver.remote.client_config import ClientConfig
        from selenium.webdriver.remote.remote_connection import RemoteConnection

        # Constructing a RemoteConnection opens no socket.
        executor = RemoteConnection(client_config=ClientConfig(
            remote_server_addr="http://127.0.0.1:1", timeout=120))
        driver = mock.Mock(command_executor=executor)

        session.apply_driver_timeout(driver)

        self.assertEqual(executor.client_config.timeout, 300)

    def _build_with_fakes(self, settings):
        from selenium import webdriver

        fake_driver = mock.Mock()
        ie_session = session.IeSession()
        # _build starts the capture proxy (IE_CAPTURE defaults to on): stop it again.
        self.addCleanup(ie_session._stop_capture)
        with mock.patch.object(webdriver, "Ie", return_value=fake_driver) as ie, \
                mock.patch.object(session, "log"), \
                mock.patch.object(session, "apply_driver_timeout") as apply_timeout:
            built = ie_session._build("http://127.0.0.1:1/", settings)
        return built, fake_driver, ie, apply_timeout

    def test_build_creates_the_driver_with_the_http_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "IEDriverServer.exe"
            exe.write_bytes(b"")
            settings = _settings({"IE_DRIVER_PATH": str(exe)})

            built, fake_driver, ie, apply_timeout = self._build_with_fakes(settings)

        self.assertIs(built, fake_driver)
        apply_timeout.assert_called_once_with(fake_driver, session.DRIVER_HTTP_TIMEOUT)

    def test_build_starts_iedriver_without_compat_layer_and_with_the_configured_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "IEDriverServer.exe"
            exe.write_bytes(b"")
            settings = _settings({"IE_DRIVER_PATH": str(exe)})

            with mock.patch.dict(os.environ, {"__COMPAT_LAYER": "X"}):
                _, _, ie, _ = self._build_with_fakes(settings)

        service = ie.call_args.kwargs["service"]
        self.assertEqual(service.path, str(exe))
        self.assertNotIn("__COMPAT_LAYER", {k.upper() for k in service.env})
        self.assertEqual(ie.call_args.kwargs["options"].unhandled_prompt_behavior, "ignore")

    def test_build_without_any_driver_tells_the_user_to_set_ie_driver_path(self):
        settings = dataclasses.replace(_settings({}), driver_path=None)

        with self.assertRaises(FileNotFoundError) as caught:
            session.IeSession()._build(None, settings)

        self.assertIn("IE_DRIVER_PATH", str(caught.exception))

    def test_build_with_a_missing_driver_file_names_ie_driver_path(self):
        settings = _settings({"IE_DRIVER_PATH": "Z:\\does\\not\\exist.exe"})

        with self.assertRaises(FileNotFoundError) as caught:
            session.IeSession()._build(None, settings)

        self.assertIn("IE_DRIVER_PATH", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
