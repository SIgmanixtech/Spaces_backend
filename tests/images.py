"""Smallest complete files of each accepted type, built byte by byte.

The upload check reads headers and end markers, so a signature followed by
padding is refused; these are structurally real files.
"""

import struct
import zlib


def png(width: int = 2, height: int = 1) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    pixels = zlib.compress(b"".join(b"\x00" + b"\x80\x80\x80" * width for _ in range(height)))
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")
    )


def jpeg(width: int = 3, height: int = 2) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = (
        b"\xff\xc0"
        + struct.pack(">HBHHB", 17, 8, height, width, 3)
        + b"\x01\x11\x00\x02\x11\x00\x03\x11\x00"
    )
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


def webp(width: int = 4, height: int = 5) -> bytes:
    bits = (width - 1) | ((height - 1) << 14)
    body = b"\x2f" + struct.pack("<I", bits) + b"\x00" * 3
    chunk = b"VP8L" + struct.pack("<I", len(body)) + body
    return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk


def pdf() -> bytes:
    return b"%PDF-1.7\n1 0 obj << >> endobj\ntrailer << >>\n%%EOF\n"
