import os
import tempfile
import unittest
import uuid
from pathlib import Path

from ie_mcp import config


# Hermetic: never scan the real ~/.cache/selenium. This home does not exist.
_NO_HOME = Path(tempfile.gettempdir()) / "ie-mcp-tests-no-such-home" / uuid.uuid4().hex


def _settings(env):
    return config.load_settings(env, home=_NO_HOME)


def _make_driver(home, arch, version):
    """Create <home>/.cache/selenium/IEDriverServer/<arch>/<version>/IEDriverServer.exe."""
    directory = Path(home) / ".cache" / "selenium" / "IEDriverServer" / arch / version
    directory.mkdir(parents=True)
    exe = directory / "IEDriverServer.exe"
    exe.write_bytes(b"")
    return exe


class DiscoverDriverPathTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)

    def test_discover_prefers_highest_win32_version(self):
        _make_driver(self.home, "win32", "4.8.0")
        _make_driver(self.home, "win32", "4.14.0")

        found = config.discover_driver_path({}, self.home)

        self.assertIn("4.14.0", found)

    def test_version_sort_is_numeric_not_textual(self):
        _make_driver(self.home, "win32", "4.9.0")
        _make_driver(self.home, "win32", "4.14.0")

        self.assertIn("4.14.0", config.discover_driver_path({}, self.home))

    def test_win32_is_preferred_over_a_newer_win64(self):
        _make_driver(self.home, "win32", "4.14.0")
        _make_driver(self.home, "win64", "4.16.0")

        found = config.discover_driver_path({}, self.home)

        self.assertIn("win32", found)
        self.assertIn("4.14.0", found)

    def test_win64_is_used_when_there_is_no_win32(self):
        _make_driver(self.home, "win64", "4.14.0")

        self.assertIn("win64", config.discover_driver_path({}, self.home))

    def test_discover_honours_explicit_env_path(self):
        explicit = self.home / "custom" / "IEDriverServer.exe"
        explicit.parent.mkdir()
        explicit.write_bytes(b"")
        _make_driver(self.home, "win32", "4.14.0")

        found = config.discover_driver_path({"IE_DRIVER_PATH": str(explicit)}, self.home)

        self.assertEqual(found, str(explicit))

    def test_discover_errors_when_explicit_path_missing(self):
        missing = self.home / "nope" / "IEDriverServer.exe"

        with self.assertRaises(FileNotFoundError) as caught:
            config.discover_driver_path({"IE_DRIVER_PATH": str(missing)}, self.home)

        self.assertIn("IE_DRIVER_PATH", str(caught.exception))

    def test_discover_returns_none_when_nothing_found(self):
        self.assertIsNone(config.discover_driver_path({}, self.home))

    def test_folder_names_with_non_ascii_digits_do_not_break_discovery(self):
        # "4.²".isdigit() is True but int() rejects it; that must not crash.
        _make_driver(self.home, "win32", "4.²")
        _make_driver(self.home, "win32", "4.8.0")

        self.assertIn("4.8.0", config.discover_driver_path({}, self.home))

    def test_directories_without_the_exe_are_ignored(self):
        _make_driver(self.home, "win32", "4.8.0")
        (self.home / ".cache" / "selenium" / "IEDriverServer" / "win32" / "4.20.0").mkdir()

        self.assertIn("4.8.0", config.discover_driver_path({}, self.home))


class SettingsTests(unittest.TestCase):
    def test_pydeps_not_added_without_env(self):
        self.assertIsNone(_settings({}).pydeps)

    def test_pydeps_is_taken_from_env(self):
        self.assertEqual(_settings({"IE_PYDEPS": "somewhere"}).pydeps, Path("somewhere"))

    def test_upload_dir_is_unset_by_default(self):
        self.assertIsNone(_settings({}).upload_dir)

    def test_upload_dir_is_taken_from_env(self):
        self.assertEqual(_settings({"IE_UPLOAD_DIR": "uploads"}).upload_dir,
                         Path("uploads").resolve())

    def test_relative_upload_dir_is_made_absolute_once_at_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous = os.getcwd()
            os.chdir(tmp)
            self.addCleanup(os.chdir, previous)
            loaded = _settings({"IE_UPLOAD_DIR": "uploads"}).upload_dir
            os.chdir(previous)

        self.assertTrue(loaded.is_absolute())
        self.assertEqual(loaded, (Path(tmp) / "uploads").resolve())

    def test_clean_session_is_opt_in(self):
        self.assertFalse(_settings({}).clean_session)
        self.assertTrue(_settings({"IE_CLEAN_SESSION": "1"}).clean_session)
        self.assertFalse(_settings({"IE_CLEAN_SESSION": "0"}).clean_session)

    def test_driver_path_comes_from_env_when_given(self):
        self.assertEqual(_settings({"IE_DRIVER_PATH": "x.exe"}).driver_path, "x.exe")


if __name__ == "__main__":
    unittest.main()
