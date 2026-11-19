"""``ie_screenshot scope=window``: PrintWindow images of the session's windows, dialogs included.

The images are returned in memory; the tests that save one write into the session's own
screenshots folder (a temporary session directory, never the repository).
"""
import base64
import ctypes
import json
import struct
import unittest
import zlib
from ctypes import wintypes

from ie_mcp import config
from tests.integration.base import IntegrationTestCase

LOOKUP = "lookup dialog"  # part of the fixture dialog's title


def window_size(hwnd):
    """``(width, height)`` of a window from ``GetWindowRect`` (the process is DPI aware)."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise OSError(f"GetWindowRect failed for {hwnd}")
    return rect.right - rect.left, rect.bottom - rect.top


def decode_png(data):
    """``(width, height, rows)`` of a PNG written by ``png.encode_png`` (8-bit RGBA, filter 0)."""
    assert data.startswith(b"\x89PNG\r\n\x1a\n"), data[:16]
    at, idat, size = 8, b"", None
    while at < len(data):
        length, kind = struct.unpack(">I4s", data[at:at + 8])
        payload = data[at + 8:at + 8 + length]
        if kind == b"IHDR":
            size = struct.unpack(">II", payload[:8])
        elif kind == b"IDAT":
            idat += payload
        at += 12 + length
    width, height = size
    raw = zlib.decompress(idat)
    stride = 1 + width * 4
    rows = [raw[i * stride + 1:(i + 1) * stride] for i in range(height)]
    return width, height, rows


def distinct_pixels(rows):
    pixels = set()
    for row in rows:
        pixels.update(row[i:i + 4] for i in range(0, len(row), 4))
    return len(pixels)


class ScreenshotTests(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(self.close_open_windows)

    # -- helpers -------------------------------------------------------------
    def images(self, result):
        return [item for item in result["content"] if item.get("type") == "image"]

    def decoded(self, item):
        self.assertEqual(item["mimeType"], "image/png")
        return decode_png(base64.b64decode(item["data"]))

    def window_shot(self, **arguments):
        result = self.call("ie_screenshot", scope="window", **arguments)
        self.assertFalse(result.get("isError"), self.texts(result))
        return result

    def open_lookup_dialog(self):
        self.close_open_windows()
        self.open_page("modal_opener.htm")
        # Native input without window focus loses about 1 click in 6 (spike D17): click again
        # until the dialog shows.
        for _ in range(4):
            # A dialog that appeared late (after the previous wait gave up) must not be opened
            # again: a second click would call showModalDialog behind the open modal.
            found = self.lookup_dialog()
            if found:
                return found
            self.text("ie_click", by="id", value="open_lookup", wait=0)
            try:
                return self.wait_window("html_dialog", 4, where=lambda w: LOOKUP in w["title"].lower())
            except AssertionError:
                continue
        found = self.lookup_dialog()
        if found:
            return found
        self.fail("the lookup dialog did not open")

    def lookup_dialog(self):
        return next((w for w in self.window_list()
                     if w["kind"] == "html_dialog" and LOOKUP in w["title"].lower()), None)

    # -- tests ---------------------------------------------------------------
    def test_window_screenshot_includes_open_dialog(self):
        dialog = self.open_lookup_dialog()
        self.text("ie_wait_element", by="id", value="pick_C3", timeout=10, target=dialog["target"])

        result = self.window_shot()

        images = self.images(result)
        self.assertEqual(len(images), 2, self.texts(result))
        main_width, main_height, main_rows = self.decoded(images[0])
        self.assertGreater(distinct_pixels(main_rows), 1, "the main window image is one colour")
        width, height, rows = self.decoded(images[1])
        expected_width, expected_height = window_size(dialog["hwnd"])
        self.assertAlmostEqual(width, expected_width, delta=expected_width * 0.1)
        self.assertAlmostEqual(height, expected_height, delta=expected_height * 0.1)
        self.assertGreater(distinct_pixels(rows), 1, "the dialog image is one colour")
        listing = "\n".join(self.texts(result))
        self.assertIn(str(dialog["hwnd"]), listing)

    def test_target_captures_only_that_window(self):
        dialog = self.open_lookup_dialog()

        result = self.window_shot(target=dialog["target"])

        self.assertEqual(len(self.images(result)), 1)
        width, height, _rows = self.decoded(self.images(result)[0])
        expected_width, expected_height = window_size(dialog["hwnd"])
        self.assertAlmostEqual(width, expected_width, delta=expected_width * 0.1)
        self.assertAlmostEqual(height, expected_height, delta=expected_height * 0.1)

    def test_window_screenshot_without_dialog_is_the_main_window(self):
        self.close_open_windows()
        self.open_page("index.htm")

        result = self.window_shot()

        images = self.images(result)
        self.assertEqual(len(images), 1, self.texts(result))
        _width, _height, rows = self.decoded(images[0])
        self.assertGreater(distinct_pixels(rows), 1)

    def test_script_alert_is_captured_without_touching_it(self):
        self.close_open_windows()
        self.open_page("dialogs.htm")
        self.text("ie_click", by="id", value="do_alert", mode="async", wait=0)
        alert = self.wait_window("script_dialog")

        result = self.window_shot()

        images = self.images(result)
        self.assertEqual(len(images), 2, self.texts(result))
        self.assertIn(str(alert["hwnd"]), "\n".join(self.texts(result)))
        # PrintWindow is a render request: the alert is still open and still answerable.
        self.assertEqual(json.loads(self.text("ie_dialog", action="text"))["text"], "Hello alert")
        self.text("ie_dialog", action="accept")
        self.wait_no_window("script_dialog")

    def test_save_path_saves_every_image_in_the_session_folder(self):
        dialog = self.open_lookup_dialog()
        folder = config.SETTINGS.session_paths.directory / "screenshots"

        result = self.window_shot(save_path="win.png", no_inline=True)

        self.assertEqual(self.images(result), [])
        self.assertTrue((folder / "win.png").is_file())
        self.assertTrue((folder / "win-2.png").is_file())
        width, height, _rows = decode_png((folder / "win-2.png").read_bytes())
        expected_width, _expected_height = window_size(dialog["hwnd"])
        self.assertAlmostEqual(width, expected_width, delta=expected_width * 0.1)

    def test_page_scope_still_uses_the_driver(self):
        self.close_open_windows()
        self.open_page("index.htm")

        result = self.call("ie_screenshot")

        self.assertFalse(result.get("isError"), self.texts(result))
        self.assertEqual(len(self.images(result)), 1)
        self.assertGreater(len(base64.b64decode(self.images(result)[0]["data"])), 100)


if __name__ == "__main__":
    unittest.main()
