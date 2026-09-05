#!/usr/bin/env python3
"""
Generate the Foxzilla app icon at every size macOS wants.

Pure standard library - a PNG is just zlib-compressed scanlines with a filter
byte, so there is no reason to pull in Pillow for a flat two-colour mark.

The mark: a charcoal rounded square with two opposing amber arrows. It reads
as "transfer" at 16px, which is the only size that really has to survive.
"""
import os
import struct
import sys
import zlib

BG = (0x1C, 0x1E, 0x21)      # charcoal
FG = (0xFF, 0xB0, 0x20)      # amber
CONCRETE = (0xDE, 0xDE, 0xDA)  # the other half of the pair


def png(width, height, rows):
    """rows: list of lists of (r,g,b,a) tuples."""
    raw = b"".join(
        b"\x00" + b"".join(bytes(px) for px in row) for row in rows
    )

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def render(size):
    s = size
    r = s * 0.22                      # corner radius
    cx = cy = (s - 1) / 2
    rows = []
    for y in range(s):
        row = []
        for x in range(s):
            # rounded-square mask
            dx = max(r - x, 0, x - (s - 1 - r))
            dy = max(r - y, 0, y - (s - 1 - r))
            inside = (dx * dx + dy * dy) <= r * r
            px = (*BG, 255) if inside else (0, 0, 0, 0)

            if inside:
                u, v = (x - cx) / s, (y - cy) / s     # -0.5 .. 0.5
                # Two opposing arrows: amber going out, concrete coming back.
                # t is distance along each arrow's own direction of travel, so
                # the same geometry serves both by flipping sign.
                for sign, colour in ((1, FG), (-1, CONCRETE)):
                    shaft_y = -0.14 * sign
                    t = u * sign
                    off = abs(v - shaft_y)
                    if -0.30 <= t <= 0.08 and off < 0.05:
                        px = (*colour, 255)          # shaft
                    elif 0.08 <= t <= 0.30 and off < 0.145 * (0.30 - t) / 0.22:
                        px = (*colour, 255)          # head, tapering to a point
            row.append(px)
        rows.append(row)
    return png(s, s, rows)


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "."
    os.makedirs(out, exist_ok=True)
    written = []
    for size in (16, 32, 64, 128, 256, 512, 1024):
        path = os.path.join(out, f"icon_{size}x{size}.png")
        with open(path, "wb") as fh:
            fh.write(render(size))
        written.append(path)
    print(f"wrote {len(written)} icons to {out}")


if __name__ == "__main__":
    main()
