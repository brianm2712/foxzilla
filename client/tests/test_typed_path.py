#!/usr/bin/env python3
"""
The path bar takes a path the way people paste it - from a terminal, with
quotes or backslash-escaped spaces - and a local site whose pinned folder is
missing (an unmounted drive) says so instead of quietly opening home.
"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import foxzilla as F

ok = True
def check(l, c, e=""):
    global ok
    print(("  PASS " if c else "  FAIL ") + l + (f"   {e}" if e else "")); ok = ok and bool(c)

WANT = "/run/media/barabus/SEAGATE EXP/dvd-rips"
c = F.clean_typed_path

check("plain path unchanged", c(WANT) == WANT)
check("double quotes", c(f'"{WANT}"') == WANT)
check("single quotes", c(f"'{WANT}'") == WANT)
check("backslash-escaped space", c(r"/run/media/barabus/SEAGATE\ EXP/dvd-rips") == WANT)
check("surrounding whitespace", c(f"  {WANT} \n") == WANT)
check("quotes and whitespace", c(f'  "{WANT}"  ') == WANT)
check("escaped parens", c(r"/m/Warrior\ \(2011\).mkv") == "/m/Warrior (2011).mkv")
check("windows path kept", c(r"C:\Users\brian") == r"C:\Users\brian")
check("lone quote kept", c('"') == '"')
check("mismatched quotes kept", c("'abc\"") == "'abc\"")

with tempfile.TemporaryDirectory() as d:
    there = F.LocalBackend(start=d)
    check("existing start folder is home", there.home() == d)
    check("existing start folder not missing", there.start_missing() is None)
    gone = os.path.join(d, "not-mounted")
    b = F.LocalBackend(start=gone)
    check("missing start folder falls back to home", b.home() == os.path.expanduser("~"))
    check("missing start folder is reported", b.start_missing() == gone)
    check("no start folder is never missing", F.LocalBackend().start_missing() is None)

print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
