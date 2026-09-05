#!/usr/bin/env python3
"""The pre-flight scan: what is already at the destination, and what to do."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import foxzilla as F

ok = True
def check(l, c, e=""):
    global ok
    print(("  PASS " if c else "  FAIL ") + l + (f"   {e}" if e else "")); ok = ok and bool(c)

L = F.LocalBackend()
src, dst = tempfile.mkdtemp(), tempfile.mkdtemp()

same = os.urandom(40000)
open(os.path.join(src, "identical.mkv"), "wb").write(same)
open(os.path.join(dst, "identical.mkv"), "wb").write(same)
open(os.path.join(src, "differs.mkv"), "wb").write(os.urandom(40000))
open(os.path.join(dst, "differs.mkv"), "wb").write(os.urandom(40000))   # same size!
open(os.path.join(src, "bigger.mkv"), "wb").write(os.urandom(90000))
open(os.path.join(dst, "bigger.mkv"), "wb").write(os.urandom(40000))
open(os.path.join(src, "brand new.mkv"), "wb").write(os.urandom(15000))

print("\ncomparing single files")
def verdict(name):
    return F.compare_paths(L, os.path.join(src, name), L, os.path.join(dst, name))
check("identical content", verdict("identical.mkv") == F.IDENTICAL, verdict("identical.mkv"))
check("same size, different content is a CONFLICT (hash caught it)",
      verdict("differs.mkv") == F.CONFLICT, verdict("differs.mkv"))
check("different size is a conflict", verdict("bigger.mkv") == F.CONFLICT)
check("absent at destination is clear", verdict("brand new.mkv") == F.CLEAR)

print("\nwhen no remote hash is possible, say 'same size' not 'identical'")
class NoHash(F.LocalBackend):
    def checksum(self, path):
        return None                 # like a chrooted sftp account or S3
check("honest about an unverified match",
      F.compare_paths(L, os.path.join(src, "identical.mkv"),
                      NoHash(), os.path.join(dst, "identical.mkv")) == F.SAME_SIZE)

print("\nplanning a directory")
os.makedirs(os.path.join(src, "sub", "deep"))
open(os.path.join(src, "sub", "deep", "x.mkv"), "wb").write(os.urandom(1000))
plan = F.plan_transfer(L, src, L, dst, True)
files = [p for p in plan if not p.is_dir]
dirs = [p for p in plan if p.is_dir]
check("every file planned", len(files) == 5, [p.name for p in files])
check("nested folders included", len(dirs) >= 3, [p.name for p in dirs])
counts = F.summarise(plan)
check("counts add up", counts[F.CLEAR] + counts[F.IDENTICAL] + counts[F.CONFLICT]
      + counts[F.SAME_SIZE] == counts["files"], counts)
check("one identical", counts[F.IDENTICAL] == 1, counts[F.IDENTICAL])
check("two conflicts", counts[F.CONFLICT] == 2, counts[F.CONFLICT])
check("two new", counts[F.CLEAR] == 2, counts[F.CLEAR])
check("new bytes counted separately", 0 < counts["new_bytes"] < counts["bytes"],
      f"{counts['new_bytes']} of {counts['bytes']}")

print("\nwhat each choice would queue")
for mode, expect in (("skip", {F.CLEAR}),
                     ("replace", {F.CLEAR, F.CONFLICT}),
                     ("all", {F.CLEAR, F.IDENTICAL, F.SAME_SIZE, F.CONFLICT})):
    wanted = F.App.WANTED[mode]
    picked = [p for p in files if p.verdict in wanted]
    check(f"'{mode}' selects {len(picked)} file(s)", set(p.verdict for p in picked) <= expect,
          sorted({p.verdict for p in picked}))
check("'skip' never overwrites anything",
      all(p.verdict == F.CLEAR for p in files if p.verdict in F.App.WANTED["skip"]))

print("\na clean destination needs no dialog")
empty = tempfile.mkdtemp()
plan2 = F.plan_transfer(L, src, L, empty, True)
c2 = F.summarise(plan2)
check("everything reads as new", c2[F.CLEAR] == c2["files"], c2)
check("nothing pre-existing", c2[F.IDENTICAL] + c2[F.CONFLICT] + c2[F.SAME_SIZE] == 0)

print(f"\n{'ALL PASS' if ok else '*** FAILURES ***'}")
sys.exit(0 if ok else 1)
