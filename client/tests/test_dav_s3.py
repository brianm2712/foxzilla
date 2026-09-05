import sys, os, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import foxzilla as F, mocks

def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  {extra}" if extra else ""))
    return bool(cond)

ok = True

# ============ WebDAV ============
dav_root = tempfile.mkdtemp()
os.makedirs(os.path.join(dav_root, "docs"))
open(os.path.join(dav_root, "docs", "note one.txt"), "w").write("dav hello\n")
open(os.path.join(dav_root, "top.bin"), "wb").write(os.urandom(70000))
_, port = mocks.serve(mocks.DAV, dav_root)

D = F.WebDAVBackend(url=f"http://127.0.0.1:{port}", user="dave", password="hunter2")
D.connect()
root = {e.name: e for e in D.listdir("/")}
ok &= check("dav listdir root", set(root) == {"docs", "top.bin"}, sorted(root))
ok &= check("dav dir flag", root["docs"].is_dir and not root["top.bin"].is_dir)
ok &= check("dav size", root["top.bin"].size == 70000, root["top.bin"].size)
ok &= check("dav mtime parsed", root["top.bin"].mtime > 0, F.when(root["top.bin"].mtime))
sub = {e.name: e for e in D.listdir("/docs")}
ok &= check("dav listdir subdir + spaces in name", "note one.txt" in sub, sorted(sub))

tmp = tempfile.mkdtemp()
D.read_to("/docs/note one.txt", os.path.join(tmp, "got.txt"))
ok &= check("dav download", open(os.path.join(tmp,"got.txt")).read() == "dav hello\n")
big = os.path.join(tmp, "up.bin"); blob = os.urandom(50000); open(big,"wb").write(blob)
D.write_from(big, "/docs/up bin.dat")
ok &= check("dav upload", open(os.path.join(dav_root,"docs","up bin.dat"),"rb").read() == blob)
D.mkdir("/newdir")
ok &= check("dav mkcol", os.path.isdir(os.path.join(dav_root,"newdir")))
D.rename("/docs/up bin.dat", "/newdir/moved.dat")
ok &= check("dav move", os.path.isfile(os.path.join(dav_root,"newdir","moved.dat")))
D.rmdir("/newdir")
ok &= check("dav delete collection", not os.path.exists(os.path.join(dav_root,"newdir")))
try:
    D.listdir("/nope"); ok &= check("dav 404 raises", False)
except F.TransferError as e:
    ok &= check("dav 404 raises", True, str(e)[:50])
bad = F.WebDAVBackend(url=f"http://127.0.0.1:{port}", user="dave", password="wrong")
try:
    bad.connect(); ok &= check("dav bad password rejected", False)
except F.TransferError as e:
    ok &= check("dav bad password rejected", True, str(e)[:40])

# ============ S3 ============
s3_root = tempfile.mkdtemp()
def wkey(k, data):
    open(os.path.join(s3_root, k.replace("/", "%2F")), "wb").write(data)
wkey("reports/2026/q1.csv", b"a,b,c\n1,2,3\n")
wkey("reports/2026/q2 final.csv", b"x\n")
wkey("reports/readme.md", b"# hi\n")
wkey("loose.txt", b"loose\n")
_, sport = mocks.serve(mocks.S3, s3_root)

S = F.S3Backend(bucket=mocks.BUCKET, access_key=mocks.AK, secret_key=mocks.SK,
                region=mocks.REGION, endpoint=f"http://127.0.0.1:{sport}")
S.connect()
ok &= check("s3 connect + SigV4 accepted by independent verifier", True)
top = {e.name: e for e in S.listdir("/")}
ok &= check("s3 list root", set(top) == {"reports", "loose.txt"}, sorted(top))
ok &= check("s3 prefix is a dir", top["reports"].is_dir and not top["loose.txt"].is_dir)
lvl = {e.name: e for e in S.listdir("/reports")}
ok &= check("s3 list prefix", set(lvl) == {"2026", "readme.md"}, sorted(lvl))
deep = {e.name: e for e in S.listdir("/reports/2026")}
ok &= check("s3 nested + space in key", set(deep) == {"q1.csv", "q2 final.csv"}, sorted(deep))
ok &= check("s3 size + mtime", deep["q1.csv"].size == 12 and deep["q1.csv"].mtime > 0,
            (deep["q1.csv"].size, F.when(deep["q1.csv"].mtime)))
S.read_to("/reports/2026/q2 final.csv", os.path.join(tmp, "s3got.txt"))
ok &= check("s3 download (signed GET on a spaced key)",
            open(os.path.join(tmp,"s3got.txt")).read() == "x\n")
S.write_from(big, "/reports/up load.bin")
ok &= check("s3 upload", open(os.path.join(s3_root,"reports%2Fup load.bin"),"rb").read() == blob)
S.rename("/reports/up load.bin", "/reports/renamed.bin")
ok &= check("s3 copy+delete rename", os.path.isfile(os.path.join(s3_root,"reports%2Frenamed.bin"))
            and not os.path.exists(os.path.join(s3_root,"reports%2Fup load.bin")))
S.remove("/reports/renamed.bin")
ok &= check("s3 delete", not os.path.exists(os.path.join(s3_root,"reports%2Frenamed.bin")))
try:
    S.read_to("/no/such.txt", os.path.join(tmp,"x")); ok &= check("s3 404 raises", False)
except F.TransferError as e:
    ok &= check("s3 404 raises", True, str(e)[:50])
bad = F.S3Backend(bucket=mocks.BUCKET, access_key=mocks.AK, secret_key="wrong-secret",
                  region=mocks.REGION, endpoint=f"http://127.0.0.1:{sport}")
try:
    bad.connect(); ok &= check("s3 wrong secret rejected", False)
except F.TransferError as e:
    ok &= check("s3 wrong secret rejected", True, str(e)[:50])

print("\nsignature rejections logged by the mock:", mocks.S3.bad_sigs or "none")
print("\n" + ("ALL PROTOCOL TESTS PASSED" if ok else "*** THERE WERE FAILURES ***"))
