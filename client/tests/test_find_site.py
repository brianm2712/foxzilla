#!/usr/bin/env python3
"""
`--check <site>` name matching: exact first, then a case-insensitive part of
the name, and never a guess between several.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import foxzilla as F

ok = True
def check(l, c, e=""):
    global ok
    print(("  PASS " if c else "  FAIL ") + l + (f"   {e}" if e else "")); ok = ok and bool(c)

SITES = [{"name": "Local"}, {"name": "DVD rips"}, {"name": "mediadrop (proxmox)"},
         {"name": "talos"}, {"name": "talos-backup"}]

def names(q):
    return [x["name"] for x in F.find_sites(SITES, q)]

check("exact name", names("mediadrop (proxmox)") == ["mediadrop (proxmox)"])
check("exact ignores case", names("LOCAL") == ["Local"])
check("part of a name", names("mediadrop") == ["mediadrop (proxmox)"])
check("part from the middle", names("proxmox") == ["mediadrop (proxmox)"])
check("exact beats a longer name containing it", names("talos") == ["talos"])
check("ambiguous part returns every match", names("tal") == ["talos", "talos-backup"])
check("no match", names("nowhere") == [])
check("blank matches nothing", names("  ") == [])

print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
