#!/usr/bin/env python3
"""
A drop folder's '.complete' marker is earned per item, not per queue.

The pickup skips its stability wait when it sees a marker, so a marker
written early makes it file a partial item (half a season). These tests pin
the rule: a batch (one item the user sent, and every job it becomes) reports
complete only once it is armed - after all its files are queued - and every
one of its jobs finished, none failed or cancelled. Unrelated jobs, and the
queue going idle while a scan is still running, have no say.
"""
import os, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import foxzilla as F

ok = True
def check(l, c, e=""):
    global ok
    print(("  PASS " if c else "  FAIL ") + l + (f"   {e}" if e else "")); ok = ok and bool(c)

class Fake(F.Backend):
    """A source whose files take `delay` seconds and can be told to fail."""
    is_local = False
    def __init__(self, delay=0.1, fail=(), listing=None):
        self.delay, self.fail, self.listing = delay, set(fail), listing or {}
    def size_of(self, path): return 10
    def listdir(self, path):
        return [F.Entry(name=n, size=10, is_dir=d) for n, d in self.listing.get(path, [])]
    def read_to(self, path, local, progress=None):
        for i in range(5):
            time.sleep(self.delay / 5)
            progress and progress((i + 1) * 2, 10, 0)
        if os.path.basename(path) in self.fail:
            raise F.TransferError("boom")
        open(local, "wb").write(b"x" * 10)

def drain(q, timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if q.idle():
            time.sleep(0.15)
            if q.idle():
                return True
        time.sleep(0.02)
    return False

def files(src, names, dst_dir):
    return [F.Job(src=src, src_path=f"/r/{n}", dst=F.LocalBackend(),
                  dst_path=os.path.join(dst_dir, n), dst_root=dst_dir) for n in names]

def recorder():
    fired = []
    return fired, (lambda b: fired.append((b.name, b.ok, b.reason)))

print("\nall jobs succeed")
q = F.Queue(lambda j: None, limit=3)
d = tempfile.mkdtemp(); fired, cb = recorder()
b = F.Batch("Season 1", on_done=cb)
q.add_batch(b, files(Fake(), ["e1", "e2", "e3"], d))
drain(q)
check("fired exactly once, ok", fired == [("Season 1", True, "")], fired)

print("\none failure spoils the batch")
fired, cb = recorder()
b = F.Batch("Season 2", on_done=cb)
q.add_batch(b, files(Fake(fail={"e2"}), ["e1", "e2", "e3"], tempfile.mkdtemp()))
drain(q)
check("fired once, not ok", len(fired) == 1 and fired[0][1] is False, fired)
check("and says why", "fail" in fired[0][2], fired[0][2])

print("\na cancelled job spoils the batch")
fired, cb = recorder()
b = F.Batch("Season 3", on_done=cb)
jobs = files(Fake(delay=0.5), ["e1", "e2", "e3"], tempfile.mkdtemp())
q2 = F.Queue(lambda j: None, limit=1)
q2.add_batch(b, jobs)
time.sleep(0.1); q2.cancel(jobs[2])                 # still queued
drain(q2)
check("not ok", len(fired) == 1 and fired[0][1] is False, fired)
check("reason names the cancellation", "cancel" in fired[0][2], fired[0][2])

print("\nthe scan race: the queue goes idle before the files are queued")
q3 = F.Queue(lambda j: None, limit=2)
fired, cb = recorder()
b = F.Batch("Film", on_done=cb)
other = files(Fake(), ["unrelated"], tempfile.mkdtemp())[0]
q3.add(other)                     # an earlier, unrelated transfer...
drain(q3)                         # ...finishes while the scan is still running
check("nothing fired while the batch had no files yet", fired == [], fired)
q3.add_batch(b, files(Fake(delay=0.3), ["part1", "part2"], tempfile.mkdtemp()))
time.sleep(0.05)
check("nothing fired while its files are still transferring", fired == [], fired)
drain(q3)
check("fired once they finished", fired == [("Film", True, "")], fired)

print("\njobs that finish before the batch is armed do not fire it")
q4 = F.Queue(lambda j: None, limit=3)
fired, cb = recorder()
b = F.Batch("Early", on_done=cb)
early = files(Fake(delay=0.05), ["a", "b"], tempfile.mkdtemp())
for j in early:
    b.add(j); q4.add(j)
drain(q4)
check("all done but unarmed: silent", fired == [], fired)
b.arm()
check("arming afterwards fires it straight away", fired == [("Early", True, "")], fired)
b.arm()
check("and only once", len(fired) == 1, fired)

print("\nfolders: children join their parent's batch")
src_dir = tempfile.mkdtemp()
os.makedirs(os.path.join(src_dir, "Show", "S01"))
for n in ("Show/a.mkv", "Show/S01/b.mkv", "Show/S01/c.mkv"):
    open(os.path.join(src_dir, n), "w").write(n)
dst_dir = tempfile.mkdtemp()
fired, cb = recorder()
b = F.Batch("Show", on_done=cb)
q5 = F.Queue(lambda j: None, limit=3)
q5.add_batch(b, [F.Job(src=F.LocalBackend(), src_path=os.path.join(src_dir, "Show"),
                       dst=F.LocalBackend(), dst_path=os.path.join(dst_dir, "Show"),
                       is_dir=True, dst_root=dst_dir)])
drain(q5)
check("fired once, ok, after the whole tree", fired == [("Show", True, "")], fired)
check("the batch owns every expanded job", len(b.jobs) == 5, [j.name for j in b.jobs])
check("and the tree really is there",
      os.path.exists(os.path.join(dst_dir, "Show", "S01", "c.mkv")))

print("\na folder with an unsafe name in it is not marked complete")
fired, cb = recorder()
b = F.Batch("Hostile", on_done=cb)
src = Fake(listing={"/r/Hostile": [("ok.mkv", False), ("../evil", False)]})
q6 = F.Queue(lambda j: None, limit=2)
dd = tempfile.mkdtemp()
q6.add_batch(b, [F.Job(src=src, src_path="/r/Hostile", dst=F.LocalBackend(),
                       dst_path=os.path.join(dd, "Hostile"), is_dir=True, dst_root=dd)])
drain(q6)
check("not ok", len(fired) == 1 and fired[0][1] is False, fired)

print("\nan empty batch, or a tainted one, is never ok")
fired, cb = recorder()
b = F.Batch("Nothing", on_done=cb)
F.Queue(lambda j: None).add_batch(b, [])
check("nothing transferred: not ok", fired and fired[0][1] is False, fired)
fired, cb = recorder()
b = F.Batch("Differs", on_done=cb)
b.taint("2 file(s) differ at the destination and were not replaced")
q7 = F.Queue(lambda j: None)
q7.add_batch(b, files(Fake(), ["x"], tempfile.mkdtemp()))
drain(q7)
check("tainted: not ok, reason kept", fired and fired[0][1] is False and "differ" in fired[0][2],
      fired)

print("\nwriting the marker")
d = tempfile.mkdtemp()
path = F.marker_path(d, "Show")
F.write_marker(F.LocalBackend(), path)
check("marker lands beside the item", os.listdir(d) == [".Show.complete"], os.listdir(d))
check("and says who wrote it", open(path).read().startswith("verified by foxzilla"))

print(f"\n{'ALL PASS' if ok else '*** FAILURES ***'}")
sys.exit(0 if ok else 1)
