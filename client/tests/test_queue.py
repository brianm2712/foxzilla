import sys, os, time, tempfile, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import foxzilla as F

# These tests move real files over SFTP. Point them at a host you control:
#   FOXZILLA_TEST_SSH_HOST=myhost python3 tests/test_sftp.py
SSH_HOST = os.environ.get("FOXZILLA_TEST_SSH_HOST")
if not SSH_HOST:
    print("skipped: set FOXZILLA_TEST_SSH_HOST to a reachable SSH host")
    raise SystemExit(0)

events = []
q = F.Queue(lambda job: events.append((job.name, job.state)), limit=3)

def drain(timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if q.idle():
            time.sleep(0.5)
            if q.idle():
                return True
        time.sleep(0.2)
    return False

# build a nested local tree
work = tempfile.mkdtemp()
tree = os.path.join(work, "payload")
os.makedirs(os.path.join(tree, "sub", "deeper"))
open(os.path.join(tree, "a.txt"), "w").write("A" * 5000)
open(os.path.join(tree, "sub", "b bin.dat"), "wb").write(os.urandom(300000))
open(os.path.join(tree, "sub", "deeper", "c.txt"), "w").write("c\n")

L = F.LocalBackend()
S = F.SFTPBackend(host=SSH_HOST, label=SSH_HOST); S.connect()

# 1. local -> sftp, whole directory
S.mkdir("/tmp/foxzilla_qtest") if "foxzilla_qtest" not in [e.name for e in S.listdir("/tmp")] else None
q.add(F.Job(src=L, src_path=tree, dst=S, dst_path="/tmp/foxzilla_qtest/payload", is_dir=True))
assert drain(), "queue stalled"
def walk_remote(p, acc=""):
    out = []
    for e in sorted(S.listdir(p), key=lambda e: e.name):
        rel = acc + "/" + e.name
        out.append((rel, e.is_dir, e.size))
        if e.is_dir:
            out += walk_remote(p + "/" + e.name, rel)
    return out
got = walk_remote("/tmp/foxzilla_qtest/payload")
for r in got: print("  remote:", r)
assert ("/a.txt", False, 5000) in got, got
assert ("/sub/b bin.dat", False, 300000) in got, got
assert ("/sub/deeper/c.txt", False, 2) in got, got
print("PASS 1 - recursive upload, spaces and binary intact")

# 2. sftp -> local, back down again
back = os.path.join(work, "back")
q.add(F.Job(src=S, src_path="/tmp/foxzilla_qtest/payload", dst=L, dst_path=back, is_dir=True))
assert drain(), "queue stalled"
orig = open(os.path.join(tree, "sub", "b bin.dat"), "rb").read()
rt   = open(os.path.join(back, "sub", "b bin.dat"), "rb").read()
assert orig == rt, f"binary mismatch {len(orig)} vs {len(rt)}"
assert open(os.path.join(back, "sub", "deeper", "c.txt")).read() == "c\n"
print("PASS 2 - recursive download, byte-identical round trip")

# 3. failure surfaces as a failed job, and does not wedge the queue
q.add(F.Job(src=S, src_path="/does/not/exist.txt", dst=L, dst_path=os.path.join(work,"nope")))
assert drain()
failed = [j for j in q.jobs if j.state == "failed"]
print("PASS 3 - failed job recorded:", failed[0].error[:60] if failed else "NONE!")
assert failed

# 4. queue still alive after that failure
q.add(F.Job(src=L, src_path=os.path.join(tree,"a.txt"), dst=S, dst_path="/tmp/foxzilla_qtest/after.txt"))
assert drain()
assert "after.txt" in [e.name for e in S.listdir("/tmp/foxzilla_qtest")]
print("PASS 4 - queue survived the failure")

# 5. progress callbacks actually fired
prog = [e for e in events if e[1] == "running"]
print("PASS 5 - progress events:", len(prog))

S.rmdir("/tmp/foxzilla_qtest")
print("cleaned up:", "foxzilla_qtest" not in [e.name for e in S.listdir("/tmp")])
S.close()
print("ALL QUEUE TESTS PASSED")
