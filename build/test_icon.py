#!/usr/bin/env python3
"""The icon generator: valid PNGs, right sizes, and legible at small sizes."""
import os, struct, sys, tempfile, zlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import icon

ok = True
def check(l, c, e=""):
    global ok
    print(("  PASS " if c else "  FAIL ") + l + (f"   {e}" if e else "")); ok = ok and bool(c)

def decode(data):
    """Pull width/height and raw RGBA rows back out of a PNG we wrote."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = struct.unpack(">II", data[16:24])
    idat = b""
    pos = 8
    while pos < len(data):
        ln = struct.unpack(">I", data[pos:pos+4])[0]
        tag = data[pos+4:pos+8]
        if tag == b"IDAT":
            idat += data[pos+8:pos+8+ln]
        pos += 12 + ln
    raw = zlib.decompress(idat)
    rows, stride = [], w * 4 + 1
    for y in range(h):
        line = raw[y*stride:(y+1)*stride]
        assert line[0] == 0, "only filter 0 is emitted"
        rows.append([tuple(line[1+x*4:5+x*4]) for x in range(w)])
    return w, h, rows

print("\nevery size renders a valid PNG")
for size in (16, 32, 64, 128, 256, 512, 1024):
    w, h, rows = decode(icon.render(size))
    if size == 16:
        check("16px decodes", (w, h) == (16, 16), f"{w}x{h}")
check("512 is 512x512", decode(icon.render(512))[:2] == (512, 512))

print("\nthe mark is actually drawn, not a blank tile")
for size in (16, 32, 64, 256):
    _, _, rows = decode(icon.render(size))
    # Count anything that is not the charcoal ground - the arrows mark is
    # half amber and half concrete, so counting only amber undersells it.
    lit = sum(1 for r in rows for px in r
              if px[3] > 200 and (px[0], px[1], px[2]) != icon.BG)
    total = sum(1 for r in rows for px in r if px[3] > 200)
    frac = lit / total if total else 0
    check(f"{size}px has a visible mark", 0.08 < frac < 0.75, f"{frac:.0%} inked")

print("\ncorners are transparent, centre is not")
_, _, rows = decode(icon.render(64))
check("top-left corner transparent", rows[0][0][3] == 0, rows[0][0])
check("centre opaque", rows[32][32][3] == 255, rows[32][32])

print("\nthe two arrows point opposite ways, in two Foxers colours")
_, _, rows = decode(icon.render(256, "arrows"))

amber = lambda px: px[0] > 200 and 120 < px[1] < 210 and px[2] < 110
pale  = lambda px: px[0] > 190 and px[1] > 190 and px[2] > 190

def mask(test):
    return {(x, y) for y in range(256) for x in range(256)
            if rows[y][x][3] > 200 and test(rows[y][x])}

a, c = mask(amber), mask(pale)
check("an amber arrow exists", len(a) > 500, f"{len(a)} px")
check("a concrete arrow exists", len(c) > 500, f"{len(c)} px")
check("the two colours do not overlap", not (a & c), len(a & c))
check("amber sits above concrete",
      sum(y for _, y in a) / len(a) < sum(y for _, y in c) / len(c))

def head_x(m):
    """Where the arrow is thickest is its head - that is the pointing end."""
    thickness = {}
    for x, y in m:
        thickness[x] = thickness.get(x, 0) + 1
    return max(thickness, key=thickness.get)

check("amber points right", head_x(a) > 128, f"head at x={head_x(a)}")
check("concrete points left", head_x(c) < 128, f"head at x={head_x(c)}")
check("both survive at 32px",
      sum(1 for r in decode(icon.render(32, "arrows"))[2]
          for px in r if px[3] > 200 and px[0] > 180) > 40)

print("\nthe fox mark still works, and sheds detail when small")
def cuts(size, mark):
    _, _, rows = decode(icon.render(size, mark))
    n = 0
    for y in range(size // 4, size * 3 // 4):
        row = rows[y]
        lit = [x for x in range(size) if row[x][0] > 200 and row[x][1] > 120]
        if len(lit) < 3:
            continue
        for x in range(lit[0], lit[-1]):
            px = row[x]
            if px[3] > 200 and px[0] < 80:
                n += 1
    return n
big, small = cuts(256, "fox") / 256**2, cuts(16, "fox") / 16**2
check("256px fox keeps eyes and muzzle", big > 0.05, f"{big:.1%} cut")
check("16px fox drops them", small < big / 3, f"{small:.1%} vs {big:.1%}")

print("\nboth marks available")
check("arrows is the default", icon.render(64) == icon.render(64, "arrows"))
check("fox still selectable", icon.MARKS["fox"] is icon._fox)
check("both render at 128", decode(icon.render(128, "fox"))[:2] == (128, 128))

print("\nwrites a full set to disk")
out = tempfile.mkdtemp()
sys.argv = ["icon.py", out]
icon.main()
files = sorted(os.listdir(out))
check("seven sizes written", len(files) == 7, files)
check("all non-empty", all(os.path.getsize(os.path.join(out, f)) > 100 for f in files))

print(f"\n{'ALL PASS' if ok else '*** FAILURES ***'}")
sys.exit(0 if ok else 1)
