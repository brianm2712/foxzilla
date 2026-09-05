#!/usr/bin/env python3
"""
Generate the Foxzilla app icon at every size macOS wants.

Pure standard library - a PNG is just zlib-compressed scanlines with a filter
byte, so there is no reason to pull in Pillow for a flat two-colour mark.

Two marks are available: "arrows" (default) - amber going out, concrete
coming back - and "fox", a geometric fox head. Both are flat colour on a charcoal rounded square, supersampled so
the diagonals survive at 16px, which is the only size that really has to.
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


def _tri(px, py, a, b, c):
    """Point-in-triangle by consistent winding sign."""
    def side(p, q, r):
        return (p[0] - r[0]) * (q[1] - r[1]) - (q[0] - r[0]) * (p[1] - r[1])
    d1, d2, d3 = side((px, py), a, b), side((px, py), b, c), side((px, py), c, a)
    neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
    pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
    return not (neg and pos)


# A fox head, built from straight edges so it stays crisp when it is 16 pixels
# across. Coordinates are in units of the icon's width, origin at centre,
# y increasing downwards.
EAR_L  = ((-0.38, -0.02), (-0.44, -0.46), (-0.07, -0.14))
EAR_R  = (( 0.38, -0.02), ( 0.44, -0.46), ( 0.07, -0.14))
HEAD   = ((-0.38, -0.06), ( 0.38, -0.06), ( 0.00,  0.44))
# Detail, in two weights. Below ~48px the fine version disappears into a
# smudge, so small icons get the bolder cuts and no muzzle at all - the same
# trick a hand-tuned iconset uses.
EYE_L      = ((-0.25, -0.02), (-0.08,  0.02), (-0.21,  0.10))
EYE_R      = (( 0.25, -0.02), ( 0.08,  0.02), ( 0.21,  0.10))
EYE_L_BOLD = ((-0.28, -0.04), (-0.05,  0.03), (-0.22,  0.15))
EYE_R_BOLD = (( 0.28, -0.04), ( 0.05,  0.03), ( 0.22,  0.15))
NOSE       = ((-0.09,  0.22), ( 0.09,  0.22), ( 0.00,  0.36))


def _fox(u, v, size=512):
    """
    Colour for a point inside the tile, or None for the background.

    Three levels of detail. Below 34px nothing survives but the silhouette,
    so the eyes and muzzle are dropped entirely and the shape has to carry
    the whole idea - which is why the notch between the ears is cut deep.
    """
    if size >= 34:
        small = size < 64
        eyes = (EYE_L_BOLD, EYE_R_BOLD) if small else (EYE_L, EYE_R)
        cuts = list(eyes) if small else [*eyes, NOSE]
        for shape in cuts:
            if _tri(u, v, *shape):
                return BG               # cut back to the ground
    if _tri(u, v, *HEAD) or _tri(u, v, *EAR_L) or _tri(u, v, *EAR_R):
        return FG
    return None


def _arrows(u, v, size=512):
    """The original mark: amber going out, concrete coming back."""
    for sign, colour in ((1, FG), (-1, CONCRETE)):
        shaft_y = -0.14 * sign
        t = u * sign
        off = abs(v - shaft_y)
        if -0.30 <= t <= 0.08 and off < 0.05:
            return colour
        if 0.08 <= t <= 0.30 and off < 0.145 * (0.30 - t) / 0.22:
            return colour
    return None


MARKS = {"arrows": _arrows, "fox": _fox}
SS = 3          # supersampling factor; 3x3 is plenty for flat colour


def render(size, mark="arrows"):
    draw = MARKS[mark]
    s = size
    r = s * 0.22
    cx = cy = (s - 1) / 2
    rows = []
    for y in range(s):
        row = []
        for x in range(s):
            # Average SSxSS samples per pixel, so the diagonals of the ears
            # and muzzle do not stair-step at small sizes.
            acc = [0, 0, 0, 0]
            for sy in range(SS):
                for sx in range(SS):
                    fx = x + (sx + 0.5) / SS
                    fy = y + (sy + 0.5) / SS
                    dx = max(r - fx, 0, fx - (s - r))
                    dy = max(r - fy, 0, fy - (s - r))
                    if (dx * dx + dy * dy) > r * r:
                        continue                    # outside the rounded square
                    u, v = (fx - cx) / s, (fy - cy) / s
                    colour = draw(u, v, s) or BG
                    acc[0] += colour[0]
                    acc[1] += colour[1]
                    acc[2] += colour[2]
                    acc[3] += 255
            n = SS * SS
            if acc[3] == 0:
                row.append((0, 0, 0, 0))
            else:
                hits = acc[3] // 255
                row.append((acc[0] // hits, acc[1] // hits,
                            acc[2] // hits, acc[3] // n))
        rows.append(row)
    return png(s, s, rows)


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "."
    mark = sys.argv[2] if len(sys.argv) > 2 else "arrows"
    os.makedirs(out, exist_ok=True)
    written = []
    for size in (16, 32, 64, 128, 256, 512, 1024):
        path = os.path.join(out, f"icon_{size}x{size}.png")
        with open(path, "wb") as fh:
            fh.write(render(size, mark))
        written.append(path)
    print(f"wrote {len(written)} '{mark}' icons to {out}")


if __name__ == "__main__":
    main()
