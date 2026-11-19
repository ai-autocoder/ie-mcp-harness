"""PrintWindow capture (fake user32/gdi32) and the window scope of ie_screenshot (fakes only).

Nothing here touches a real window, a real GDI object or a browser: ``SESSION.ensure`` raises,
the enumeration and the capture are replaced.
"""
import contextlib
import ctypes
import dataclasses
import io
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from ie_mcp import backends, protocol
import ie_mcp.tools  # noqa: F401  (importing the package registers every tool)
from ie_mcp.session import SESSION
from ie_mcp.tools import read as readtools
from ie_mcp.win32 import capture, messages, windows
from ie_mcp.win32.windows import WindowInfo
from tests.test_tool_guards import ToolGuardTestCase, _call


# --- capture_window with fake user32 / gdi32 ----------------------------------------------------
class FakeApi:
    """user32 + gdi32 stand-ins that record every call, and can fail one of them.

    Handles are small ints. ``pixels`` is the BGRA buffer ``CreateDIBSection`` hands out and
    ``PrintWindow`` fills, so the code under test reads real memory.
    """

    SCREEN, MEMORY, DIB, OLD = 11, 22, 33, 44

    def __init__(self, width=3, height=2, fail=None, bgra=None, rect_at=(100, 50)):
        self.log, self.fail = [], fail
        self.width, self.height = width, height
        self.rect_at = rect_at
        self.pixels = ctypes.create_string_buffer(bgra or bytes(width * height * 4), width * height * 4)
        self.bgra = bgra
        self.header = None
        self.user32, self.gdi32 = self, self

    def step(self, name, *args, result=1):
        self.log.append((name, *args))
        if self.fail == name:
            raise OSError(f"{name} failed")
        return result

    # user32
    def GetWindowRect(self, hwnd, rect):
        self.step("GetWindowRect", hwnd)
        left, top = self.rect_at
        rect.contents.left, rect.contents.top = left, top
        rect.contents.right, rect.contents.bottom = left + self.width, top + self.height
        return 1

    def GetDC(self, hwnd):
        return self.step("GetDC", hwnd, result=self.SCREEN)

    def ReleaseDC(self, hwnd, hdc):
        return self.step("ReleaseDC", hwnd, hdc)

    def PrintWindow(self, hwnd, hdc, flags):
        if self.fail == "PrintWindow-zero":
            self.log.append(("PrintWindow", hwnd, hdc, flags))
            return 0
        if self.bgra:
            ctypes.memmove(self.pixels, self.bgra, len(self.bgra))
        return self.step("PrintWindow", hwnd, hdc, flags)

    # gdi32
    def CreateCompatibleDC(self, hdc):
        return self.step("CreateCompatibleDC", hdc, result=self.MEMORY)

    def CreateDIBSection(self, hdc, bmi, usage, bits, section, offset):
        self.header = bmi.contents.bmiHeader
        self.step("CreateDIBSection", hdc)
        if self.fail == "CreateDIBSection-null":
            return None
        bits.contents.value = ctypes.addressof(self.pixels)
        return self.DIB

    def SelectObject(self, hdc, obj):
        return self.step("SelectObject", hdc, obj, result=self.OLD)

    def DeleteObject(self, obj):
        return self.step("DeleteObject", obj)

    def DeleteDC(self, hdc):
        return self.step("DeleteDC", hdc)

    def names(self):
        return [entry[0] for entry in self.log]


class CaptureWindowTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(capture, "_log")  # keep failures out of the real session log
        self.log = patcher.start()
        self.addCleanup(patcher.stop)

    def test_capture_converts_bgra_to_rgba_with_opaque_alpha(self):
        # PrintWindow leaves alpha undefined: two pixels with alpha 0 and 7.
        bgra = bytes([1, 2, 3, 0, 10, 20, 30, 7])
        api = FakeApi(width=2, height=1, bgra=bgra)

        width, height, rgba = capture.capture_window(1234, api=api)

        self.assertEqual((width, height), (2, 1))
        self.assertEqual(rgba, bytes([3, 2, 1, 255, 30, 20, 10, 255]))

    def test_capture_uses_a_top_down_32_bit_dib_and_full_content_flag(self):
        api = FakeApi(width=3, height=2)

        capture.capture_window(1234, api=api)

        self.assertEqual((api.header.biWidth, api.header.biHeight, api.header.biBitCount),
                         (3, -2, 32))
        self.assertIn(("PrintWindow", 1234, api.MEMORY, 2), api.log)
        # the DIB was selected into the memory DC and the old object restored afterwards
        self.assertEqual(api.log.index(("SelectObject", api.MEMORY, api.DIB)),
                         api.names().index("SelectObject"))

    def test_capture_releases_everything_on_success(self):
        api = FakeApi()

        capture.capture_window(1234, api=api)

        self.assertEqual(api.names()[-4:], ["SelectObject", "DeleteObject", "DeleteDC", "ReleaseDC"])
        self.assertIn(("SelectObject", api.MEMORY, api.OLD), api.log)
        self.assertIn(("DeleteObject", api.DIB), api.log)
        self.assertIn(("DeleteDC", api.MEMORY), api.log)
        self.assertIn(("ReleaseDC", None, api.SCREEN), api.log)

    def test_print_window_failure_raises_and_releases_everything(self):
        api = FakeApi(fail="PrintWindow-zero")

        with self.assertRaises(RuntimeError) as caught:
            capture.capture_window(1234, api=api)

        self.assertIn("PrintWindow", str(caught.exception))
        self.assertEqual(api.names()[-4:], ["SelectObject", "DeleteObject", "DeleteDC", "ReleaseDC"])
        self.assertIn(("SelectObject", api.MEMORY, api.OLD), api.log)

    def test_error_after_each_acquisition_releases_what_was_acquired(self):
        cases = {
            "CreateCompatibleDC": [("ReleaseDC", None, FakeApi.SCREEN)],
            "CreateDIBSection": [("DeleteDC", FakeApi.MEMORY), ("ReleaseDC", None, FakeApi.SCREEN)],
            "CreateDIBSection-null": [("DeleteDC", FakeApi.MEMORY),
                                      ("ReleaseDC", None, FakeApi.SCREEN)],
            "PrintWindow": [("SelectObject", FakeApi.MEMORY, FakeApi.OLD),
                            ("DeleteObject", FakeApi.DIB), ("DeleteDC", FakeApi.MEMORY),
                            ("ReleaseDC", None, FakeApi.SCREEN)],
        }
        for fail, released in cases.items():
            with self.subTest(fail=fail):
                api = FakeApi(fail=fail)
                with self.assertRaises((OSError, RuntimeError)):
                    capture.capture_window(1234, api=api)
                for entry in released:
                    self.assertIn(entry, api.log)
                # nothing that was never acquired is released
                if fail in ("CreateCompatibleDC",):
                    self.assertNotIn("DeleteDC", api.names())
                if fail.startswith("CreateDIBSection"):
                    self.assertNotIn("DeleteObject", api.names())

    def test_select_object_failure_still_releases(self):
        api = FakeApi(fail="SelectObject")

        with self.assertRaises(OSError):
            capture.capture_window(1234, api=api)

        self.assertIn(("DeleteObject", api.DIB), api.log)
        self.assertIn(("DeleteDC", api.MEMORY), api.log)
        self.assertIn(("ReleaseDC", None, api.SCREEN), api.log)

    def test_a_window_without_area_is_refused_before_any_gdi_object_exists(self):
        api = FakeApi(width=0, height=5)

        with self.assertRaises(ValueError):
            capture.capture_window(1234, api=api)

        self.assertNotIn("GetDC", api.names())

    def test_an_absurdly_large_window_is_refused(self):
        api = FakeApi(width=20000, height=20000)

        with self.assertRaises(ValueError):
            capture.capture_window(1234, api=api)

        self.assertNotIn("GetDC", api.names())

    def test_one_release_failing_does_not_stop_the_later_releases(self):
        # The capture itself succeeded; DeleteObject then raises while releasing.
        api = FakeApi(fail="DeleteObject")

        width, height, rgba = capture.capture_window(1234, api=api)

        self.assertEqual((width, height, len(rgba)), (3, 2, 24))
        self.assertIn(("DeleteDC", api.MEMORY), api.log)
        self.assertIn(("ReleaseDC", None, api.SCREEN), api.log)
        self.assertTrue(any("DeleteObject" in str(call) for call in self.log.call_args_list))

    def test_a_failing_release_does_not_hide_the_real_error(self):
        api = FakeApi(fail="PrintWindow-zero")
        original = api.DeleteObject

        def broken(obj):
            original(obj)
            raise OSError("DeleteObject failed")
        api.DeleteObject = broken

        with self.assertRaises(RuntimeError) as caught:
            capture.capture_window(1234, api=api)

        self.assertIn("PrintWindow", str(caught.exception))
        self.assertIn(("ReleaseDC", None, api.SCREEN), api.log)

    def test_bgra_to_rgba(self):
        self.assertEqual(capture.bgra_to_rgba(bytes([1, 2, 3, 4])), bytes([3, 2, 1, 255]))
        self.assertEqual(capture.bgra_to_rgba(b""), b"")


class CliDpiTests(unittest.TestCase):
    def test_main_enables_dpi_awareness_before_anything_else(self):
        from ie_mcp import cli
        order = []
        for argv, name in ((["ie-mcp", "--selftest"], "selftest"), (["ie-mcp", "--install"], "install")):
            order.clear()
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(
                    cli.capture, "enable_dpi_awareness", side_effect=lambda: order.append("dpi")))
                stack.enter_context(mock.patch.object(
                    cli, name, side_effect=lambda *a, _name=name, **k: order.append(_name) or 0))
                stack.enter_context(mock.patch("sys.argv", argv))
                stack.enter_context(self.assertRaises(SystemExit))
                cli.main()
            self.assertEqual(order, ["dpi", name])

    def test_main_serve_path_enables_dpi_awareness_first(self):
        from ie_mcp import cli
        order = []
        paths = mock.Mock(session_id="s", directory="d")
        with contextlib.ExitStack() as stack:
            for patcher in (
                mock.patch.object(cli.capture, "enable_dpi_awareness",
                                  side_effect=lambda: order.append("dpi")),
                mock.patch.object(cli, "ensure_ie_policies", side_effect=lambda: order.append("policies")),
                mock.patch.object(cli, "acquire_lock", side_effect=lambda: order.append("lock")),
                mock.patch.object(cli, "log"),
                mock.patch.object(cli, "serve"),
                mock.patch.object(cli.SESSION, "close"),
                mock.patch.object(cli.config, "SETTINGS", mock.Mock(session_paths=paths)),
                mock.patch("sys.argv", ["ie-mcp"]),
                mock.patch("sys.stdin"),
                mock.patch("sys.stdout"),
            ):
                stack.enter_context(patcher)
            cli.main()
        self.assertEqual(order[0], "dpi")


class WindowsHelperTests(unittest.TestCase):
    """``root_window`` and ``window_pid`` with a fake user32 (no real window is asked)."""

    def fake_user32(self, **functions):
        return types.SimpleNamespace(**functions)

    def test_root_window_is_getancestor_ga_root(self):
        calls = []
        user32 = self.fake_user32(GetAncestor=lambda hwnd, flag: calls.append((hwnd, flag)) or 900)
        with mock.patch.object(windows, "_user32", return_value=user32):
            self.assertEqual(windows.root_window(5), 900)
        self.assertEqual(calls, [(5, 2)])  # GA_ROOT

    def test_root_window_is_zero_for_a_dead_window_or_without_user32(self):
        user32 = self.fake_user32(GetAncestor=lambda hwnd, flag: None)
        with mock.patch.object(windows, "_user32", return_value=user32):
            self.assertEqual(windows.root_window(5), 0)
        with mock.patch.object(windows, "_user32", return_value=None):
            self.assertEqual(windows.root_window(5), 0)

    def test_window_pid_reads_the_owning_process(self):
        def get_pid(hwnd, out):
            out._obj.value = 4242 if hwnd == 7 else 0
            return 1 if hwnd == 7 else 0
        user32 = self.fake_user32(GetWindowThreadProcessId=get_pid)
        with mock.patch.object(windows, "_user32", return_value=user32):
            self.assertEqual(windows.window_pid(7), 4242)
            self.assertIsNone(windows.window_pid(8))
        with mock.patch.object(windows, "_user32", return_value=None):
            self.assertIsNone(windows.window_pid(7))


# --- the window scope of ie_screenshot ---------------------------------------------------------------
MAIN, DIALOG, ALERT, SYSTEM = 1001, 1002, 1003, 1004


def info(hwnd, kind, cls="Chrome_WidgetWin_1", title="", visible=True):
    return WindowInfo(hwnd=hwnd, pid=100, cls=cls, title=title, kind=kind, owner_hwnd=None,
                      visible=visible)


# EnumWindows order: the top-most window first.
LISTING = [info(ALERT, "script_dialog", "#32770", "Message"),
           info(SYSTEM, "system_dialog", "#32770", "Security"),
           info(DIALOG, "html_dialog", "Internet Explorer_TridentDlgFrame", "Lookup"),
           info(MAIN, "main", title="Main")]


class WindowScopeTests(ToolGuardTestCase):
    def setUp(self):
        super().setUp()
        self.captured, self.minimized, self.failing = [], set(), set()
        self.dead, self.pid_of = set(), {}  # windows that no longer exist; hwnd -> process id
        self.listing = list(LISTING)
        self.root = {}

        def fake_capture(hwnd):
            self.captured.append(hwnd)
            if hwnd in self.failing:
                raise RuntimeError("PrintWindow failed")
            width, height = 2 + hwnd % 10, 3
            return width, height, bytes([hwnd % 251]) * (width * height * 4)

        for patcher in (
            mock.patch.object(SESSION, "main_hwnd", MAIN),
            mock.patch.object(windows, "session_window_pids", return_value={100}),
            mock.patch.object(windows, "enumerate_windows", side_effect=lambda *a, **k: list(self.listing)),
            mock.patch.object(windows, "root_window", side_effect=lambda h: self.root.get(h, h)),
            mock.patch.object(capture, "capture_window", side_effect=fake_capture),
            mock.patch.object(capture, "is_minimized", side_effect=lambda h: h in self.minimized),
            mock.patch.object(messages, "is_window", side_effect=lambda h: h not in self.dead),
            mock.patch.object(windows, "window_pid", side_effect=lambda h: self.pid_of.get(h, 100)),
            mock.patch.object(windows, "class_name", return_value="FakeClass"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        # window scope must never reach the browser
        ensure = mock.patch.object(SESSION, "ensure", side_effect=AssertionError("ensure called"))
        self.ensure = ensure.start()
        self.addCleanup(ensure.stop)
        self.addCleanup(setattr, backends, "DEFAULT_TARGET", backends.DEFAULT_TARGET)
        backends.DEFAULT_TARGET = None

    def shot(self, **arguments):
        return _call("ie_screenshot", dict({"scope": "window"}, **arguments))["result"]

    @staticmethod
    def images(result):
        return [i for i in result["content"] if i["type"] == "image"]

    @staticmethod
    def texts(result):
        return [i["text"] for i in result["content"] if i["type"] == "text"]

    def test_window_scope_main_then_dialogs_bottom_to_top(self):
        result = self.shot()

        self.assertFalse(result.get("isError"), result)
        self.assertEqual(self.captured, [MAIN, DIALOG, ALERT])
        self.assertEqual(len(self.images(result)), 3)
        self.ensure.assert_not_called()

    def test_listing_names_every_window_and_never_captures_a_system_dialog(self):
        text = "\n".join(self.texts(self.shot()))

        for hwnd in (MAIN, DIALOG, ALERT, SYSTEM):
            self.assertIn(str(hwnd), text)
        self.assertIn("system_dialog", text)
        self.assertIn("not captured", text)
        self.assertNotIn(SYSTEM, self.captured)

    def test_window_scope_does_not_take_the_driver(self):
        spec = protocol.REGISTRY["ie_screenshot"]
        self.assertFalse(spec.needs_driver({"scope": "window"}))
        self.assertFalse(spec.needs_driver({"target": "hwnd:5"}))
        self.assertFalse(spec.needs_driver({"target": "tab:5"}))  # refused by the tool itself
        self.assertFalse(spec.needs_driver({"scope": "bogus"}))
        self.assertTrue(spec.needs_driver({}))
        self.assertTrue(spec.needs_driver({"scope": "page"}))
        self.assertTrue(spec.needs_driver({"by": "id", "value": "x"}))

    def test_no_session_window_is_a_clear_error(self):
        with mock.patch.object(SESSION, "main_hwnd", None):
            result = self.shot()

        self.assertTrue(result.get("isError"))
        self.assertIn("ie_open", self.texts(result)[0])
        self.assertEqual(self.captured, [])

    def test_attached_tab_default_target_uses_the_tab_top_window(self):
        backends.DEFAULT_TARGET = "tab:7"
        self.root[7] = 2000
        patcher = mock.patch.object(windows, "ATTACHED_TOPS", {2000})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.listing = [info(2000, "main", title="Tab owner")] + self.listing

        result = self.shot()

        self.assertEqual(self.captured[0], 2000)
        self.assertNotIn(MAIN, self.captured)
        self.assertFalse(result.get("isError"), result)

    def test_a_minimized_window_is_reported_not_restored(self):
        self.minimized.add(MAIN)

        result = self.shot()

        self.assertNotIn(MAIN, self.captured)
        self.assertEqual(self.captured, [DIALOG, ALERT])
        self.assertIn("minimized, not captured", "\n".join(self.texts(result)))

    def test_hidden_dialogs_are_ignored(self):
        self.listing[0] = info(ALERT, "script_dialog", "#32770", "Message", visible=False)

        self.shot()

        self.assertEqual(self.captured, [MAIN, DIALOG])

    def test_one_window_failing_does_not_hide_the_others(self):
        self.failing.add(DIALOG)

        result = self.shot()

        self.assertFalse(result.get("isError"), result)
        self.assertEqual(len(self.images(result)), 2)
        self.assertIn("PrintWindow failed", "\n".join(self.texts(result)))

    def test_every_capture_failing_is_an_error(self):
        self.failing.update({MAIN, DIALOG, ALERT})

        result = self.shot()

        self.assertTrue(result.get("isError"))
        self.assertIn("PrintWindow failed", self.texts(result)[0])

    def test_target_captures_that_one_window_only(self):
        result = self.shot(target=f"hwnd:{DIALOG}")

        self.assertEqual(self.captured, [DIALOG])
        self.assertEqual(len(self.images(result)), 1)

    def test_target_is_enough_without_scope(self):
        result = _call("ie_screenshot", {"target": f"hwnd:{ALERT}"})["result"]

        self.assertEqual(self.captured, [ALERT])
        self.assertEqual(len(self.images(result)), 1)

    def test_target_may_be_the_main_window_or_an_attached_top(self):
        self.listing = [w for w in self.listing if w.hwnd != MAIN]  # not enumerated
        self.shot(target=f"hwnd:{MAIN}")
        with mock.patch.object(windows, "ATTACHED_TOPS", {777}):
            self.shot(target="hwnd:777")

        self.assertEqual(self.captured, [MAIN, 777])

    def test_target_outside_the_session_is_refused(self):
        for target in ("hwnd:99999", "tab:5", "driver", "hwnd:0", "hwnd:abc", "5", f"hwnd:{SYSTEM}"):
            with self.subTest(target=target):
                result = self.shot(target=target)
                self.assertTrue(result.get("isError"), result)
        self.assertEqual(self.captured, [])

    def test_a_non_page_window_is_refused_as_target(self):
        self.listing.append(info(555, "other"))

        result = self.shot(target="hwnd:555")

        self.assertTrue(result.get("isError"))
        self.assertEqual(self.captured, [])

    def test_conflicting_arguments_are_errors(self):
        for arguments in ({"scope": "bogus"}, {"scope": "window", "by": "id", "value": "x"},
                          {"scope": "page", "target": f"hwnd:{MAIN}"},
                          {"scope": "element", "target": f"hwnd:{MAIN}", "by": "id", "value": "x"},
                          {"scope": "element"}, {"scope": "page", "value": "x"}):
            with self.subTest(arguments=arguments):
                result = _call("ie_screenshot", arguments)["result"]
                self.assertTrue(result.get("isError"), result)
        self.assertEqual(self.captured, [])
        self.ensure.assert_not_called()

    def test_save_path_names_the_first_image_and_numbers_the_rest(self):
        paths = self.use_settings()

        result = self.shot(save_path="shot.png", no_inline=True)

        folder = paths.directory / "screenshots"
        self.assertEqual(sorted(p.name for p in folder.iterdir()),
                         ["shot-2.png", "shot-3.png", "shot.png"])
        self.assertEqual(self.images(result), [])  # no_inline: text only
        text = "\n".join(self.texts(result))
        for name in ("shot.png", "shot-2.png", "shot-3.png"):
            self.assertIn(str(folder / name), text)
        self.assertTrue((folder / "shot.png").read_bytes().startswith(b"\x89PNG"))

    def test_a_bad_save_path_is_refused_before_anything_is_captured(self):
        self.use_settings()

        for bad in ("..\\x.png", "sub\\x.png", "", "con", "x*.png"):
            with self.subTest(save_path=bad):
                result = self.shot(save_path=bad)
                self.assertTrue(result.get("isError"), result)
        self.assertEqual(self.captured, [])

    def test_save_path_with_one_image_uses_the_plain_name(self):
        paths = self.use_settings()

        self.shot(target=f"hwnd:{MAIN}", save_path="only.png")

        self.assertEqual([p.name for p in (paths.directory / "screenshots").iterdir()],
                         ["only.png"])

    # -- the session's main and attached top windows are re-checked (stale hwnds) ---------------
    def test_a_main_window_that_no_longer_exists_is_not_captured(self):
        # Edge was killed from outside: SESSION.main_hwnd is stale and the number can be reused.
        self.dead.add(MAIN)

        result = self.shot()

        self.assertTrue(result.get("isError"))
        self.assertIn("ie_open", self.texts(result)[0])
        self.assertEqual(self.captured, [])
        self.ensure.assert_not_called()

    def test_a_recycled_main_hwnd_of_another_process_is_not_captured(self):
        self.pid_of[MAIN] = 999  # the number now names an unrelated window
        self.listing = [w for w in self.listing if w.hwnd != MAIN]

        for arguments in ({}, {"target": f"hwnd:{MAIN}"}):
            with self.subTest(arguments=arguments):
                result = self.shot(**arguments)
                self.assertTrue(result.get("isError"), result)
        self.assertEqual(self.captured, [])

    def test_a_stale_main_hwnd_is_refused_as_target(self):
        self.dead.add(MAIN)
        self.listing = [w for w in self.listing if w.hwnd != MAIN]

        result = self.shot(target=f"hwnd:{MAIN}")

        self.assertTrue(result.get("isError"))
        self.assertEqual(self.captured, [])

    def test_a_dead_attached_top_is_refused(self):
        with mock.patch.object(windows, "ATTACHED_TOPS", {777}):
            self.dead.add(777)
            result = self.shot(target="hwnd:777")
        self.assertTrue(result.get("isError"))
        self.assertEqual(self.captured, [])

    def test_a_tab_top_that_is_not_an_attached_top_is_refused(self):
        backends.DEFAULT_TARGET = "tab:7"
        self.root[7] = 2000  # some window; ATTACHED_TOPS does not know it

        result = self.shot()

        self.assertTrue(result.get("isError"))
        self.assertEqual(self.captured, [])

    def test_a_dead_tab_top_is_refused(self):
        backends.DEFAULT_TARGET = "tab:7"
        self.root[7] = 2000
        with mock.patch.object(windows, "ATTACHED_TOPS", {2000}):
            self.dead.add(2000)
            result = self.shot()
        self.assertTrue(result.get("isError"))
        self.assertEqual(self.captured, [])

    # -- a write error is not a capture error ---------------------------------------------------
    def test_a_save_failure_is_reported_and_the_image_is_still_returned(self):
        self.use_settings()

        with mock.patch.object(readtools, "open", create=True, side_effect=OSError("disk full")):
            result = self.shot(save_path="shot.png")

        self.assertFalse(result.get("isError"), result)
        self.assertEqual(len(self.images(result)), 3)  # captured, so returned
        text = "\n".join(self.texts(result))
        self.assertIn("could not save", text)
        self.assertIn("disk full", text)
        self.assertNotIn("capture failed", text)

    def test_default_tab_helper_is_public(self):
        backends.DEFAULT_TARGET = "tab:9"
        self.assertEqual(backends.default_tab_hwnd(), 9)
        backends.DEFAULT_TARGET = "hwnd:9"
        self.assertIsNone(backends.default_tab_hwnd())
        backends.DEFAULT_TARGET = None
        self.assertIsNone(backends.default_tab_hwnd())

    def test_page_and_element_scopes_still_use_the_driver(self):
        # SESSION.ensure raises in setUp; swap in the guard test's fake driver for this one
        ensure = mock.patch.object(SESSION, "ensure", side_effect=lambda *a, **k: self.driver)
        ensure.start()
        self.addCleanup(ensure.stop)
        element = mock.Mock(screenshot_as_png=self.driver.get_screenshot_as_png())
        find = mock.patch.object(SESSION, "find", return_value=element)
        find.start()
        self.addCleanup(find.stop)

        page = _call("ie_screenshot", {"scope": "page"})["result"]
        element = _call("ie_screenshot", {"by": "id", "value": "x"})["result"]
        explicit = _call("ie_screenshot", {"scope": "element", "by": "id", "value": "x"})["result"]

        for result in (page, element, explicit):
            self.assertEqual(self.images(result)[0]["mimeType"], "image/png")
        self.assertEqual(self.captured, [])

    def test_schema_documents_scope_and_target(self):
        properties = protocol.REGISTRY["ie_screenshot"].input_schema["properties"]

        self.assertEqual(properties["scope"]["enum"], ["page", "element", "window"])
        self.assertIn("target", properties)


if __name__ == "__main__":
    unittest.main()
