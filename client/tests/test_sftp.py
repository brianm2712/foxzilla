import sys, os, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import foxzilla as F

# These tests move real files over SFTP. Point them at a host you control:
#   FOXZILLA_TEST_SSH_HOST=myhost python3 tests/test_sftp.py
SSH_HOST = os.environ.get("FOXZILLA_TEST_SSH_HOST")
if not SSH_HOST:
    print("skipped: set FOXZILLA_TEST_SSH_HOST to a reachable SSH host")
    raise SystemExit(0)
s = F.SFTPBackend(host=SSH_HOST, label=SSH_HOST); s.connect()

def expect_fail(what, fn):
    try:
        fn(); print(f"FAIL - {what} should have raised")
    except F.TransferError as e:
        print(f"ok  - {what}: {str(e)[:70]}")

expect_fail("listdir missing dir", lambda: s.listdir("/does/not/exist"))
expect_fail("get missing file", lambda: s.read_to("/does/not/exist.txt", "/tmp/x.txt"))
expect_fail("put to unwritable path", lambda: s.write_from(__file__, "/does/not/exist/x.py"))
expect_fail("mkdir under missing parent", lambda: s.mkdir("/does/not/exist/d"))
expect_fail("rm missing", lambda: s.remove("/does/not/exist.txt"))

# happy paths still work
tmpd = tempfile.mkdtemp(); src = os.path.join(tmpd, "ok.txt")
open(src, "w").write("still fine\n")
s.write_from(src, "/tmp/foxzilla_ok.txt")
s.read_to("/tmp/foxzilla_ok.txt", os.path.join(tmpd, "b.txt"))
print("ok  - round trip:", repr(open(os.path.join(tmpd,"b.txt")).read()))
print("ok  - listdir still works:", len(s.listdir("/etc")) > 10)
s.remove("/tmp/foxzilla_ok.txt")
print("ok  - cleaned up")
s.close()
