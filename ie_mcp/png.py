"""A minimal PNG encoder (stdlib only: ``zlib`` and ``struct``)."""
import struct
import zlib

SIGNATURE = b"\x89PNG\r\n\x1a\n"
BYTES_PER_PIXEL = 4
COMPRESSION_LEVEL = 6  # screenshots are large and mostly flat: the default trade-off is fine


def _chunk(kind: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)


def encode_png(width: int, height: int, rgba: bytes) -> bytes:
    """An 8-bit RGBA PNG (colour type 6, filter 0 on every row, no interlace).

    ``rgba`` is ``width * height * 4`` bytes, top row first.
    """
    if width <= 0 or height <= 0:
        raise ValueError(f"image size must be positive, got {width}x{height}")
    stride = width * BYTES_PER_PIXEL
    if len(rgba) != stride * height:
        raise ValueError(f"expected {stride * height} bytes for {width}x{height} RGBA, "
                         f"got {len(rgba)}")
    # One zero filter byte in front of each row.
    raw = bytearray()
    for row in range(height):
        raw.append(0)
        raw += rgba[row * stride:(row + 1) * stride]
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (SIGNATURE + _chunk(b"IHDR", header)
            + _chunk(b"IDAT", zlib.compress(bytes(raw), COMPRESSION_LEVEL))
            + _chunk(b"IEND", b""))
