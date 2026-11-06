#!/usr/bin/env python3
"""
Cancel stops the transfer, on every backend, and leaves things usable.

- FTP: a cancelled download/upload aborts and reconnects, so the next
  command on that backend works and is not reading the tail of the old one.
- FTP: one backend used from two threads at once (browsing while the queue
  transfers) never interleaves commands on the control connection.
- S3/WebDAV: uploads stream from disk and stop when cancelled.
- The queue downloads to '<name>.part' and renames only on success; a
  cancelled or failed download leaves neither file behind.
- Queue.remove_finished() drops finished jobs under the lock.
Loopback mocks; run outside a sandbox that forbids binding.
"""
import hashlib, os, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import foxzilla as F, ftpmock, mocks

ok = True
def check(l, c, e=""):
    global ok
    print(("  PASS " if c else "  FAIL ") + l + (f"   {e}" if e else "")); ok = ok and bool(c)

def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()

def cancel_after(n):
    calls = []
    def progress(done, total, rate=0.0):
        calls.append(done)
        if len(calls) >= n:
            raise F.TransferError("cancelled")
    return progress

BIG = os.urandom(6 * 1024 * 1024)

# ---------------------------------------------------------------- FTP -------
root = tempfile.mkdtemp()
open(os.path.join(root, "big.bin"), "wb").write(BIG)
open(os.path.join(root, "small.txt"), "w").write("small\n")
port = ftpmock.serve(root)
B = F.FTPBackend(host="127.0.0.1", port=port, user=ftpmock.USER, password=ftpmock.PASSWORD)
B.connect()
tmp = tempfile.mkdtemp()

print("\nFTP download cancelled part-way")
try:
    B.read_to("/big.bin", os.path.join(tmp, "a.bin"), cancel_after(2))
    check("raised", False)
except F.TransferError as e:
    check("raised", "cancel" in str(e), str(e))
check("the backend still lists correctly afterwards",
      {e.name for e in B.listdir("/")} == {"big.bin", "small.txt"},
      sorted(e.name for e in B.listdir("/")))
B.read_to("/big.bin", os.path.join(tmp, "b.bin"))
check("and a full download afterwards is intact", sha(os.path.join(tmp, "b.bin"))
      == hashlib.sha256(BIG).hexdigest())

print("\nFTP upload cancelled part-way")
up = os.path.join(tmp, "up.bin"); open(up, "wb").write(BIG)
try:
    B.write_from(up, "/up.bin", cancel_after(2))
    check("raised", False)
except F.TransferError as e:
    check("raised", "cancel" in str(e), str(e))
B.read_to("/small.txt", os.path.join(tmp, "s.txt"))
check("the next command works", open(os.path.join(tmp, "s.txt")).read() == "small\n")

print("\nFTP: two threads on one backend")
errors, results = [], {}
def browse():
    try:
        for _ in range(15):
            results.setdefault("lists", []).append(sorted(e.name for e in B.listdir("/")))
    except Exception as e:                                   # noqa: BLE001
        errors.append(("browse", e))
def fetch():
    try:
        for i in range(3):
            B.read_to("/big.bin", os.path.join(tmp, f"t{i}.bin"))
    except Exception as e:                                   # noqa: BLE001
        errors.append(("fetch", e))
ts = [threading.Thread(target=browse), threading.Thread(target=fetch)]
[t.start() for t in ts]; [t.join(60) for t in ts]
check("no errors", not errors, errors[:2])
lists = results.get("lists", [])
check("every listing was right", len(lists) == 15 and
      all({"big.bin", "small.txt"} <= set(l) for l in lists), lists[:2])
check("every download was intact", all(sha(os.path.join(tmp, f"t{i}.bin")) ==
      hashlib.sha256(BIG).hexdigest() for i in range(3)))
B.close()

# ------------------------------------------------------------ S3 / DAV ------
print("\nWebDAV upload cancelled part-way")
dav_root = tempfile.mkdtemp()
_, dport = mocks.serve(mocks.DAV, dav_root)
D = F.WebDAVBackend(url=f"http://127.0.0.1:{dport}", user="dave", password="hunter2")
D.connect()
try:
    D.write_from(up, "/up.bin", cancel_after(2))
    check("raised", False)
except F.TransferError as e:
    check("raised", "cancel" in str(e), str(e))
p = os.path.join(dav_root, "up.bin")
check("the server did not get the whole file",
      not os.path.exists(p) or os.path.getsize(p) < len(BIG),
      os.path.getsize(p) if os.path.exists(p) else "absent")
seen = []
D.write_from(up, "/whole.bin", lambda d, t, r=0: seen.append(d))
check("an uncancelled upload still arrives whole", sha(os.path.join(dav_root, "whole.bin"))
      == hashlib.sha256(BIG).hexdigest())
check("and reports progress as it goes, not just at the end", len(set(seen)) > 3, len(set(seen)))

print("\nS3 upload cancelled part-way")
s3_root = tempfile.mkdtemp()
_, sport = mocks.serve(mocks.S3, s3_root)
S = F.S3Backend(bucket=mocks.BUCKET, access_key=mocks.AK, secret_key=mocks.SK,
                region=mocks.REGION, endpoint=f"http://127.0.0.1:{sport}")
try:
    S.write_from(up, "/up.bin", cancel_after(2))
    check("raised", False)
except F.TransferError as e:
    check("raised", "cancel" in str(e), str(e))
check("nothing stored", not os.path.exists(os.path.join(s3_root, "up.bin")))
seen = []
S.write_from(up, "/whole.bin", lambda d, t, r=0: seen.append(d))
check("an uncancelled upload is stored whole and correctly signed",
      sha(os.path.join(s3_root, "whole.bin")) == hashlib.sha256(BIG).hexdigest(),
      mocks.S3.bad_sigs[-1:] if mocks.S3.bad_sigs else "")
check("with progress along the way", len(set(seen)) > 3, len(set(seen)))

# ------------------------------------------------------- the queue: .part ---
class Trickle(F.Backend):
    """A source that writes slowly, and remembers where it was told to write."""
    is_local = False
    def __init__(self, fail=False):
        self.paths = []; self.fail = fail
    def size_of(self, path): return 1000
    def read_to(self, path, local, progress=None):
        self.paths.append(local)
        with open(local, "wb") as fh:
            for i in range(10):
                fh.write(b"x" * 100); fh.flush()
                if self.fail and i == 5:
                    raise F.TransferError("server went away")
                progress and progress((i + 1) * 100, 1000, 0)
                time.sleep(0.05)

def run(job, cancel_at=None):
    q = F.Queue(lambda j: None, limit=1)
    q.add(job)
    t0 = time.time()
    while not q.idle() and time.time() - t0 < 20:
        if cancel_at and time.time() - t0 > cancel_at:
            q.cancel(job); cancel_at = None
        time.sleep(0.02)
    return q

print("\nthe queue downloads to .part, then renames")
d = tempfile.mkdtemp(); final = os.path.join(d, "movie.mkv")
src = Trickle()
run(F.Job(src=src, src_path="/r/movie.mkv", dst=F.LocalBackend(), dst_path=final, dst_root=d))
check("wrote to <name>.part", src.paths == [final + ".part"], src.paths)
check("final file present and complete", os.path.getsize(final) == 1000)
check("no .part left", os.listdir(d) == ["movie.mkv"], os.listdir(d))

print("\na cancelled download leaves nothing")
d = tempfile.mkdtemp(); final = os.path.join(d, "movie.mkv")
job = F.Job(src=Trickle(), src_path="/r/movie.mkv", dst=F.LocalBackend(), dst_path=final)
run(job, cancel_at=0.15)
check("job cancelled", job.state == "cancelled", job.state)
check("no file and no .part", os.listdir(d) == [], os.listdir(d))

print("\na failed download leaves nothing, and an existing file is untouched")
d = tempfile.mkdtemp(); final = os.path.join(d, "movie.mkv")
open(final, "w").write("the old good copy")
job = F.Job(src=Trickle(fail=True), src_path="/r/movie.mkv", dst=F.LocalBackend(), dst_path=final)
run(job)
check("job failed", job.state == "failed", (job.state, job.error))
check("old file intact", open(final).read() == "the old good copy")
check("no .part left", os.listdir(d) == ["movie.mkv"], os.listdir(d))

print("\nremove_finished")
q = F.Queue(lambda j: None, limit=1)
q.set_paused(True)
jobs = [F.Job(src=Trickle(), src_path=f"/r/{n}", dst=F.LocalBackend(), dst_path="/x")
        for n in "abcde"]
for j, st in zip(jobs, ["done", "failed", "cancelled", "skipped", "queued"]):
    j.state = st; q.jobs.append(j)
gone = q.remove_finished()
check("drops done, cancelled and skipped", sorted(j.name for j in gone) == ["a", "c", "d"],
      [j.name for j in gone])
check("keeps failed and queued", [j.name for j in q.jobs] == ["b", "e"], [j.name for j in q.jobs])

print(f"\n{'ALL PASS' if ok else '*** FAILURES ***'}")
sys.exit(0 if ok else 1)
