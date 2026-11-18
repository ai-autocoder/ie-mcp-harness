import dataclasses
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ie_mcp import config, protocol
import ie_mcp.tools  # noqa: F401  (importing the package registers every tool)
from ie_mcp.session import SESSION

PNG = b"\x89PNG\r\n\x1a\nfake"


def _call(name, arguments=None):
    return protocol.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": name, "arguments": arguments or {}}})


def _text(response):
    return response["result"]["content"][0]["text"]


class FakeSwitchTo:
    def default_content(self):
        pass


class FakeDriver:
    """Stands in for the Selenium driver; nothing here starts a browser."""

    def __init__(self, script_result="", png=PNG):
        self.switch_to = FakeSwitchTo()
        self._script_result = script_result
        self._png = png
        self.scripts = []

    def execute_script(self, script, *args):
        self.scripts.append(script)
        return self._script_result

    def get_screenshot_as_png(self):
        return self._png

    def find_element(self, by, value):
        raise AssertionError("not expected")


class ToolGuardTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        # A guard that fails open must not litter the repository with files.
        previous = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, previous)
        self.session_dir = self.tmp / "sessions"
        self.upload_dir = self.tmp / "uploads"
        self.upload_dir.mkdir()
        self.driver = FakeDriver()
        # Keep the server log (stderr and file) out of the test output and the real temp dir.
        stderr = mock.patch("sys.stderr", io.StringIO())
        stderr.start()
        self.addCleanup(stderr.stop)
        self.use_settings()
        # Every tool reaches the browser only through SESSION: replace both entry points so a
        # failing guard can never start Edge.
        self.sent_keys = []
        element = mock.Mock()
        element.send_keys.side_effect = self.sent_keys.append
        for patcher in (
            mock.patch.object(SESSION, "ensure", side_effect=lambda *a, **k: self.driver),
            mock.patch.object(SESSION, "find", return_value=element),
            mock.patch.object(SESSION, "_apply_frame"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def use_settings(self, **changes):
        original = config.SETTINGS
        paths = config.build_session_paths(env={"IE_SESSION_ROOT": str(self.session_dir)}, pid=1)
        config.SETTINGS = dataclasses.replace(original, session_paths=paths, **changes)
        self.addCleanup(setattr, config, "SETTINGS", original)
        return paths


class UploadGuardTests(ToolGuardTestCase):
    def _upload(self, path):
        return _call("ie_upload", {"by": "id", "value": "f", "path": path})

    def test_upload_disabled_without_upload_dir(self):
        self.use_settings(upload_dir=None)

        response = self._upload(str(self.upload_dir / "a.txt"))

        self.assertTrue(response["result"]["isError"])
        self.assertIn("IE_UPLOAD_DIR", _text(response))
        self.assertEqual(self.sent_keys, [])

    def test_upload_rejects_path_outside_upload_dir(self):
        self.use_settings(upload_dir=self.upload_dir)

        response = self._upload("..\\x.txt")

        self.assertTrue(response["result"]["isError"])
        self.assertIn("outside IE_UPLOAD_DIR", _text(response))
        self.assertEqual(self.sent_keys, [])

    def test_upload_rejects_absolute_path_outside_upload_dir(self):
        self.use_settings(upload_dir=self.upload_dir)
        outside = self.tmp / "secret.txt"
        outside.write_text("x")

        response = self._upload(str(outside))

        self.assertTrue(response["result"]["isError"])
        self.assertIn("outside IE_UPLOAD_DIR", _text(response))

    def test_upload_rejects_a_sibling_directory_sharing_the_name_prefix(self):
        # A string-prefix check would accept "uploads-evil" for "uploads".
        self.use_settings(upload_dir=self.upload_dir)
        sibling = self.tmp / "uploads-evil"
        sibling.mkdir()
        (sibling / "a.txt").write_text("x")

        response = self._upload(str(sibling / "a.txt"))

        self.assertTrue(response["result"]["isError"])
        self.assertIn("outside IE_UPLOAD_DIR", _text(response))

    def test_upload_accepts_a_relative_path_inside_upload_dir(self):
        self.use_settings(upload_dir=self.upload_dir)
        (self.upload_dir / "a.txt").write_text("x")

        response = self._upload("a.txt")

        self.assertNotIn("isError", response["result"])
        self.assertEqual(self.sent_keys, [str((self.upload_dir / "a.txt").resolve())])

    def test_upload_accepts_an_absolute_path_inside_upload_dir(self):
        self.use_settings(upload_dir=self.upload_dir)
        target = self.upload_dir / "sub" / "b.txt"
        target.parent.mkdir()
        target.write_text("x")

        response = self._upload(str(target))

        self.assertNotIn("isError", response["result"])
        self.assertEqual(self.sent_keys, [str(target.resolve())])

    def test_upload_of_a_missing_file_is_an_error(self):
        self.use_settings(upload_dir=self.upload_dir)

        response = self._upload("missing.txt")

        self.assertTrue(response["result"]["isError"])
        self.assertEqual(self.sent_keys, [])


class ScreenshotGuardTests(ToolGuardTestCase):
    def test_screenshot_save_path_must_be_relative(self):
        paths = self.use_settings()
        shots = paths.directory / "screenshots"

        for bad in ("C:\\x.png", "..\\x.png", "C:x.png", "sub\\x.png", "sub/x.png",
                    "/x.png", "..", ".", "", "a:b.png", "x*.png", "NUL.png", "shot.", "shot ",
                    "COM¹.png", "lpt²", "con"):
            with self.subTest(save_path=bad):
                response = _call("ie_screenshot", {"save_path": bad, "no_inline": True})
                self.assertTrue(response["result"].get("isError"), response)
        self.assertFalse(shots.exists() and any(shots.iterdir()))

    def test_screenshot_saved_under_session_screenshots_folder(self):
        paths = self.use_settings()

        response = _call("ie_screenshot", {"save_path": "shot.png", "no_inline": True})

        self.assertNotIn("isError", response["result"])
        saved = paths.directory / "screenshots" / "shot.png"
        self.assertEqual(saved.read_bytes(), PNG)
        self.assertIn(str(saved), _text(response))

    def test_close_keeps_the_screenshots_folder(self):
        paths = self.use_settings()
        _call("ie_screenshot", {"save_path": "shot.png", "no_inline": True})

        with mock.patch.object(SESSION, "quit"):
            _call("ie_close")
        # And the real cleanup path never removes it either.
        from ie_mcp import session
        session.cleanup_stale_sessions(paths.root, browser_rows=[], pid_alive=lambda pid: False)

        self.assertTrue((paths.directory / "screenshots" / "shot.png").exists())


class ScreenshotSchemaTests(unittest.TestCase):
    def test_schema_says_an_empty_save_path_is_an_error(self):
        schema = protocol.REGISTRY["ie_screenshot"].input_schema
        self.assertIn("empty", schema["properties"]["save_path"]["description"])


class TextFallbackTests(ToolGuardTestCase):
    def test_text_falls_back_to_inner_text(self):
        class Body:
            @property
            def text(self):
                raise RuntimeError("atom failed")

        driver = FakeDriver(script_result="inner text of body")
        driver.find_element = lambda by, value: Body()
        self.driver = driver

        response = _call("ie_text")

        self.assertNotIn("isError", response["result"])
        self.assertEqual(_text(response), "inner text of body")
        self.assertEqual(driver.scripts, ["return document.body.innerText"])

    def test_text_uses_the_element_text_when_it_works(self):
        class Body:
            text = "plain text"

        driver = FakeDriver(script_result="should not be used")
        driver.find_element = lambda by, value: Body()
        self.driver = driver

        self.assertEqual(_text(_call("ie_text")), "plain text")
        self.assertEqual(driver.scripts, [])


class FramesTests(ToolGuardTestCase):
    def test_frames_lists_name_id_src(self):
        self.driver = FakeDriver(
            script_result="0\x1fmenu\x1fmenu\x1fmenu.htm\x1e1\x1fmain\x1f\x1fform.htm")

        response = _call("ie_frames")

        import json
        self.assertEqual(json.loads(_text(response)), [
            {"index": 0, "name": "menu", "id": "menu", "src": "menu.htm"},
            {"index": 1, "name": "main", "id": "", "src": "form.htm"},
        ])

    def test_frames_with_no_frames_is_an_empty_list(self):
        self.driver = FakeDriver(script_result="")

        self.assertEqual(_text(_call("ie_frames")), "[]")

    def test_frames_script_is_the_frames_js_resource(self):
        from ie_mcp import js
        self.driver = FakeDriver(script_result="")

        _call("ie_frames")

        self.assertEqual(self.driver.scripts, [js.load("frames.js")])


class OpenDescriptionTests(unittest.TestCase):
    def test_open_says_an_http_url_is_required(self):
        description = protocol.REGISTRY["ie_open"].description

        self.assertIn("http(s)", description)
        self.assertIn("about:blank", description)


if __name__ == "__main__":
    unittest.main()
