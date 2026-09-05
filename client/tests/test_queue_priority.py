#!/usr/bin/env python3
"""Priority reordering, parallelism limits and skip-if-identical."""
import os, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import foxzilla as F

ok = True
def check(l, c, e=""):
    global ok
    print(("  PASS " if c else "  FAIL ") + l + (f"   {e}" if e else "")); ok = ok and bool(c)

class SlowBackend(F.Backend):
    """A fake backend that records the order and concurrency of transfers."""
    is_local = False
    def __init__(self, label="slow", delay=0.4, max_parallel=4):
        self.label = label; self.delay = delay; self.max_parallel = max_parallel
        self.order = []; self.live = 0; self.peak = 0; self.lock = threading.Lock()
    def listdir(self, path): return []
    def mkdir(self, path): pass
    def size_of(self, path): return 100
    def read_to(self, path, local, progress=None, resume=False):
        with self.lock:
            self.order.append(os.path.basename(path))
            self.live += 1; self.peak = max(self.peak, self.live)
        for i in range(4):
            time.sleep(self.delay / 4)
            progress and progress((i + 1) * 25, 100, 1000.0)
        with self.lock: self.live -= 1
        open(local, "wb").write(b"x" * 100)
    def write_from(self, local, path, progress=None, resume=False):
        self.read_to(path, tempfile.mkstemp()[1], progress)

def new_queue(**kw):
    ev = []
    return F.Queue(lambda j: ev.append((j.name, j.state)), **kw), ev

print("\npriority: moving a job to the top runs it first")
src = SlowBackend(delay=0.5); dst = F.LocalBackend()
q, _ = new_queue(limit=1)          # serial, so order is observable
q.set_paused(True)
out = tempfile.mkdtemp()
jobs = [F.Job(src=src, src_path=f"/r/{n}.bin", dst=dst,
              dst_path=os.path.join(out, f"{n}.bin")) for n in ("a", "b", "c", "d")]
for j in jobs: q.add(j)
q.move_to(jobs[3], 0)              # promote 'd'
q.move(jobs[2], -1)                # nudge 'c' up one
q.set_paused(False)
t0 = time.time()
while not q.idle() and time.time() - t0 < 60: time.sleep(0.1)
check("promoted job ran first", src.order and src.order[0] == "d.bin", src.order)
check("all four ran", len(src.order) == 4, src.order)
check("nudged job overtook", src.order.index("c.bin") < src.order.index("b.bin"), src.order)

print("\nparallelism honours the queue limit")
src2 = SlowBackend(delay=0.6, max_parallel=8); dst2 = F.LocalBackend()
q2, _ = new_queue(limit=3)
out2 = tempfile.mkdtemp()
for n in range(8):
    q2.add(F.Job(src=src2, src_path=f"/r/{n}.bin", dst=dst2,
                 dst_path=os.path.join(out2, f"{n}.bin")))
t0 = time.time()
while not q2.idle() and time.time() - t0 < 90: time.sleep(0.1)
check("ran several at once", src2.peak > 1, f"peak {src2.peak}")
check("never exceeded the limit of 3", src2.peak <= 3, f"peak {src2.peak}")
check("all eight completed", len(src2.order) == 8, len(src2.order))

print("\na single-connection backend stays serial")
src3 = SlowBackend(delay=0.3, max_parallel=1); dst3 = F.LocalBackend()
q3, _ = new_queue(limit=6)
out3 = tempfile.mkdtemp()
for n in range(5):
    q3.add(F.Job(src=src3, src_path=f"/r/{n}.bin", dst=dst3,
                 dst_path=os.path.join(out3, f"{n}.bin")))
t0 = time.time()
while not q3.idle() and time.time() - t0 < 60: time.sleep(0.1)
check("max_parallel=1 respected", src3.peak == 1, f"peak {src3.peak}")
check("FTP backend declares itself serial", F.FTPBackend.max_parallel == 1)

print("\nskip-if-identical")
L = F.LocalBackend()
a, b = tempfile.mkdtemp(), tempfile.mkdtemp()
blob = os.urandom(50000)
open(os.path.join(a, "same.bin"), "wb").write(blob)
open(os.path.join(b, "same.bin"), "wb").write(blob)
open(os.path.join(a, "diff.bin"), "wb").write(blob)
open(os.path.join(b, "diff.bin"), "wb").write(os.urandom(50000))
q4, _ = new_queue(limit=2, skip_identical=True)
j_same = F.Job(src=L, src_path=os.path.join(a, "same.bin"), dst=L,
               dst_path=os.path.join(b, "same.bin"))
j_diff = F.Job(src=L, src_path=os.path.join(a, "diff.bin"), dst=L,
               dst_path=os.path.join(b, "diff.bin"))
q4.add(j_same); q4.add(j_diff)
t0 = time.time()
while not q4.idle() and time.time() - t0 < 60: time.sleep(0.1)
check("identical file skipped", j_same.state == "skipped", j_same.state)
check("differing file transferred", j_diff.state == "done", j_diff.state)
check("differing file now matches source",
      open(os.path.join(b, "diff.bin"), "rb").read() == blob)

print("\npause and cancel")
src5 = SlowBackend(delay=0.4); dst5 = F.LocalBackend()
q5, _ = new_queue(limit=1)
q5.set_paused(True)
out5 = tempfile.mkdtemp()
held = [F.Job(src=src5, src_path=f"/r/p{n}.bin", dst=dst5,
              dst_path=os.path.join(out5, f"p{n}.bin")) for n in range(3)]
for j in held: q5.add(j)
time.sleep(1.0)
check("paused queue starts nothing", all(j.state == "queued" for j in held),
      [j.state for j in held])
q5.cancel(held[1])
check("cancelling a queued job marks it", held[1].state == "cancelled")
q5.set_paused(False)
t0 = time.time()
while not q5.idle() and time.time() - t0 < 60: time.sleep(0.1)
check("cancelled job never ran", "p1.bin" not in src5.order, src5.order)
check("the others ran", len(src5.order) == 2, src5.order)

print("\nprogress carries a transfer rate")
src6 = SlowBackend(delay=0.4); dst6 = F.LocalBackend()
q6, ev6 = new_queue(limit=1)
j6 = F.Job(src=src6, src_path="/r/z.bin", dst=dst6,
           dst_path=os.path.join(tempfile.mkdtemp(), "z.bin"))
q6.add(j6)
t0 = time.time()
while not q6.idle() and time.time() - t0 < 60: time.sleep(0.1)
check("rate recorded", j6.rate > 0, f"{j6.rate}")
check("progress bar renders", F.bar(50, 100) and "50%" in F.bar(50, 100), F.bar(50, 100))
check("empty bar for unknown size", F.bar(0, 0) == "")

print(f"\n{'ALL PASS' if ok else '*** FAILURES ***'}")
sys.exit(0 if ok else 1)
