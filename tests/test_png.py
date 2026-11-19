"""PNG encoder, DPI awareness and the PrintWindow capture (fakes only: no real window, no GDI)."""
import ctypes
import struct
import unittest
import zlib
from unittest import mock

from ie_mcp import png
from ie_mcp.win32 import capture

SIGNATURE = b"\x89PNG\r\n\x1a\n"


def chunks(data):
    """``[(type, payload)]`` of a PNG, CRCs checked."""
    assert data.startswith(SIGNATURE)
    out, at = [], len(SIGNATURE)
    while at < len(data):
        length, kind = struct.unpack(">I4s", data[at:at + 8])
        payload = data[at + 8:at + 8 + length]
        (crc,) = struct.unpack(">I", data[at + 8 + length:at + 12 + length])
        assert crc == zlib.crc32(kind + payload) & 0xFFFFFFFF, kind
        out.append((kind, payload))
        at += 12 + length
    return out


RGBA_3X2 = bytes(range(24))  # 3 x 2 pixels, 4 bytes each


class EncodePngTests(unittest.TestCase):
    def test_encode_png_signature_and_ihdr(self):
        data = png.encode_png(3, 2, RGBA_3X2)

        self.assertTrue(data.startswith(SIGNATURE))
        kind, payload = chunks(data)[0]
        self.assertEqual(kind, b"IHDR")
        width, height, depth, colour, compression, filtering, interlace = struct.unpack(
            ">IIBBBBB", payload)
        self.assertEqual((width, height), (3, 2))
        self.assertEqual((depth, colour, compression, filtering, interlace), (8, 6, 0, 0, 0))
        self.assertEqual(chunks(data)[-1], (b"IEND", b""))

    def test_encode_png_roundtrip_pixels(self):
        data = png.encode_png(3, 2, RGBA_3X2)

        idat = b"".join(payload for kind, payload in chunks(data) if kind == b"IDAT")
        raw = zlib.decompress(idat)
        stride = 3 * 4
        self.assertEqual(len(raw), 2 * (1 + stride))
        rows = [raw[i * (1 + stride):(i + 1) * (1 + stride)] for i in range(2)]
        self.assertEqual([row[0] for row in rows], [0, 0])  # filter type 0 on every row
        self.assertEqual(b"".join(row[1:] for row in rows), RGBA_3X2)

    def test_encode_png_rejects_a_wrong_buffer_size(self):
        with self.assertRaises(ValueError):
            png.encode_png(3, 2, RGBA_3X2[:-1])
        with self.assertRaises(ValueError):
            png.encode_png(0, 2, b"")


class FakeFunction:
    """A callable that accepts attribute assignment (argtypes/restype), like a ctypes function."""

    def __init__(self, result=1):
        self.result = result
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.result


class FakeUser32:
    def __init__(self, **functions):
        for name, fn in functions.items():
            setattr(self, name, fn)


class DpiAwarenessTests(unittest.TestCase):
    def setUp(self):
        # The failure path logs: keep it out of the real session log file.
        patcher = mock.patch.object(capture, "_log")
        self.log = patcher.start()
        self.addCleanup(patcher.stop)

    def test_dpi_awareness_declares_pointer_argtype(self):
        fn = FakeFunction(1)
        user32 = FakeUser32(SetProcessDpiAwarenessContext=fn)

        self.assertTrue(capture.enable_dpi_awareness(user32))

        self.assertEqual(fn.argtypes, [ctypes.c_void_p])
        (argument,), = fn.calls
        self.assertIsInstance(argument, ctypes.c_void_p)
        self.assertEqual(argument.value, ctypes.c_void_p(-4).value)  # PER_MONITOR_AWARE_V2

    def test_dpi_awareness_failure_returns_false(self):
        self.assertFalse(capture.enable_dpi_awareness(
            FakeUser32(SetProcessDpiAwarenessContext=FakeFunction(0))))  # call refused
        self.assertFalse(capture.enable_dpi_awareness(FakeUser32()))  # function missing

        def boom(*args):
            raise OSError("access violation")
        self.assertFalse(capture.enable_dpi_awareness(
            FakeUser32(SetProcessDpiAwarenessContext=boom)))
        self.assertEqual(self.log.call_count, 3)  # every failure is logged


if __name__ == "__main__":
    unittest.main()
