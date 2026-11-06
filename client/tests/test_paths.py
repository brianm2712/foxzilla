#!/usr/bin/env python3
"""
Server-supplied names can't write outside the folder you picked.

A hostile FTP server (or a shared S3 bucket, or a WebDAV share) chooses the
names in its listings. A recursive download joins those names onto a local
folder, so '../evil' or '/Users/me/.zshrc' would land outside it. Driven
against the loopback FTP mock with its listing doctored.
"""
import os, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import foxzilla as F, ftpmock

ok = True
def check(l, c, e=""):
    global ok
    print(("  PASS " if c else "  FAIL ") + l + (f"   {e}" if e else "")); ok = ok and bool(c)

print("\nsafe_name")
for good in ["a.txt", "Season 1", "..hidden", "a..b", ".config", "x" * 200]:
    check(f"accepts {good[:20]!r}", F.safe_name(good) == good)
for bad in ["", ".", "..", "../evil", "a/../../b", "/etc/passwd", "sub/x",
            "a\\..\\b", "nul\x00byte", "C:\\boot.ini"]:
    try:
        F.safe_name(bad); check(f"refuses {bad!r}", False, "accepted")
    except F.TransferError:
        check(f"refuses {bad!r}", True)

print("\nunder_root")
root = tempfile.mkdtemp()
check("a child is under the root", F.under_root(root, os.path.join(root, "a", "b")))
check("the root itself counts", F.under_root(root, root))
check("a sibling with a shared prefix is not", not F.under_root(root, root + "x/y"))
check("dot-dot out is not", not F.under_root(root, os.path.join(root, "..", "x")))
os.symlink("/", os.path.join(root, "escape"))
check("a symlink out of the root is not (local)",
      not F.under_root(root, os.path.join(root, "escape", "etc", "x"), local=True))
check("posix remote paths too", F.under_root("/srv/drop", "/srv/drop/a")
      and not F.under_root("/srv/drop", "/srv/drop/../etc"))

# -- a hostile FTP server ------------------------------------------------------
srv_root = tempfile.mkdtemp()
os.makedirs(os.path.join(srv_root, "pub"))
open(os.path.join(srv_root, "pub", "fine.txt"), "w").write("fine\n")
port = ftpmock.serve(srv_root)

B = F.FTPBackend(host="127.0.0.1", port=port, user=ftpmock.USER, password=ftpmock.PASSWORD)
B.connect()
real_mlsd = B.ftp.mlsd
EVIL = ["../evil", "/tmp/foxzilla-abs-evil", "..", "sub/../../evil2"]
def doctored(path, facts=None):
    yield from real_mlsd(path, facts=facts)
    if path.rstrip("/").endswith("pub"):
        for name in EVIL:
            yield name, {"type": "file", "size": "5", "modify": "20260101000000"}
B.ftp.mlsd = doctored

parent = tempfile.mkdtemp()
dest = os.path.join(parent, "chosen")
os.mkdir(dest)

print("\nscan of a hostile listing")
plan = F.plan_transfer(B, "/pub", F.LocalBackend(), os.path.join(dest, "pub"), True)
names = {p.name: p.verdict for p in plan if not p.is_dir}
check("the honest file is planned", names.get("fine.txt") == F.CLEAR, names)
check("hostile names are planned as unsafe, never as clear",
      all(v == F.UNSAFE for n, v in names.items() if n != "fine.txt") and len(names) > 1,
      names)
check("no choice in the dialog ever sends an unsafe item",
      all(F.UNSAFE not in w for w in F.WANTED.values()))
check("summarise counts them", F.summarise(plan)[F.UNSAFE] >= 3, F.summarise(plan))

print("\nqueued recursive download of a hostile listing")
q = F.Queue(lambda j: None, limit=1)
top = F.Job(src=B, src_path="/pub", dst=F.LocalBackend(),
            dst_path=os.path.join(dest, "pub"), is_dir=True, dst_root=dest)
q.add(top)
t0 = time.time()
while not q.idle() and time.time() - t0 < 30:
    time.sleep(0.05)
by_name = {j.name: j for j in q.jobs}
check("the honest file arrived", open(os.path.join(dest, "pub", "fine.txt")).read() == "fine\n")
check("nothing was written beside the chosen folder", sorted(os.listdir(parent)) == ["chosen"],
      os.listdir(parent))
check("nothing landed at the absolute path", not os.path.exists("/tmp/foxzilla-abs-evil"))
bad_jobs = [j for j in q.jobs if j is not top and j.name != "fine.txt"]
check("each hostile entry became a failed job", bad_jobs and
      all(j.state == "failed" and "unsafe" in j.error for j in bad_jobs),
      [(j.src_path, j.state, j.error) for j in bad_jobs])

print("\na destination outside its root is refused at transfer time")
q2 = F.Queue(lambda j: None, limit=1)
src_file = os.path.join(tempfile.mkdtemp(), "a.txt")
open(src_file, "w").write("a")
j = F.Job(src=F.LocalBackend(), src_path=src_file, dst=F.LocalBackend(),
          dst_path=os.path.join(dest, "..", "outside.txt"), dst_root=dest)
q2.add(j)
t0 = time.time()
while not q2.idle() and time.time() - t0 < 10:
    time.sleep(0.05)
check("job failed", j.state == "failed" and "outside" in j.error, (j.state, j.error))
check("and wrote nothing", not os.path.exists(os.path.join(parent, "outside.txt")))

B.close()
print(f"\n{'ALL PASS' if ok else '*** FAILURES ***'}")
sys.exit(0 if ok else 1)
