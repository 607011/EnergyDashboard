#!/usr/bin/env python3
"""Draws the app icons for the home-screen app (a sun on a dark background), using only the stdlib.

    python3 scripts/make-pwa-icons.py

Writes PNGs to caddy/pwa/. Full-bleed squares: iOS and Android round or mask the corners
themselves. The "maskable" variant keeps everything inside the central safe zone.
"""
import math
import struct
import zlib
from pathlib import Path

BACKGROUND = (20, 22, 27)
SUN = (245, 179, 26)
SUPERSAMPLE = 3


def render(size: int, scale: float) -> bytes:
    """RGB rows for a size x size icon; `scale` shrinks the sun (1.0 = full, <1 = safe-zone)."""
    big = size * SUPERSAMPLE
    c = big / 2
    core = 0.21 * big * scale
    ray_in, ray_out = 0.30 * big * scale, 0.42 * big * scale
    ray_half = 0.028 * big * scale
    rays = [(math.cos(a * math.pi / 6), math.sin(a * math.pi / 6)) for a in range(12)]

    def covered(x: float, y: float) -> bool:
        dx, dy = x - c, y - c
        r = math.hypot(dx, dy)
        if r <= core:
            return True
        if not ray_in <= r <= ray_out:
            return False
        # distance from the point to the nearest ray's centre line
        return any(abs(dx * ry - dy * rx) <= ray_half and dx * rx + dy * ry > 0 for rx, ry in rays)

    rows = []
    for py in range(size):
        row = bytearray([0])  # PNG filter type 0
        for px in range(size):
            hits = sum(
                covered(px * SUPERSAMPLE + sx + 0.5, py * SUPERSAMPLE + sy + 0.5)
                for sy in range(SUPERSAMPLE)
                for sx in range(SUPERSAMPLE)
            )
            t = hits / (SUPERSAMPLE * SUPERSAMPLE)
            row += bytes(round(BACKGROUND[i] + (SUN[i] - BACKGROUND[i]) * t) for i in range(3))
        rows.append(bytes(row))
    return b"".join(rows)


def write_png(path: Path, size: int, scale: float) -> None:
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(render(size, scale), 9))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)
    print(f"{path}  {size}x{size}  {len(png)} bytes")


if __name__ == "__main__":
    out = Path(__file__).resolve().parent.parent / "caddy" / "pwa"
    out.mkdir(parents=True, exist_ok=True)
    write_png(out / "apple-touch-icon.png", 180, 1.0)
    write_png(out / "icon-192.png", 192, 1.0)
    write_png(out / "icon-512.png", 512, 1.0)
    write_png(out / "icon-maskable-512.png", 512, 0.72)
