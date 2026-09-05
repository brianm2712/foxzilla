import sys, os, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import foxzilla as F, ftpmock

ok = True
def check(label, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + label + (f"  {extra}" if extra else ""))
    ok = ok and bool(cond)

root = tempfile.mkdtemp()
os.makedirs(os.path.join(root, "pub", "inner"))
open(os.path.join(root, "pub", "readme one.txt"), "w").write("ftp hello\n")
open(os.path.join(root, "big.bin"), "wb").write(os.urandom(120000))
port = ftpmock.serve(root)

B = F.FTPBackend(host="127.0.0.1", port=port, user=ftpmock.USER, password=ftpmock.PASSWORD)
B.connect()
check("ftp connect + home", B.home() == "/", B.home())
top = {e.name: e for e in B.listdir("/")}
check("ftp listdir (MLSD)", set(top) == {"pub", "big.bin"}, sorted(top))
check("ftp dir flag + size", top["pub"].is_dir and top["big.bin"].size == 120000)
check("ftp mtime", top["big.bin"].mtime > 0, F.when(top["big.bin"].mtime))
sub = {e.name: e for e in B.listdir("/pub")}
check("ftp subdir + spaces", set(sub) == {"inner", "readme one.txt"}, sorted(sub))

tmp = tempfile.mkdtemp()
B.read_to("/pub/readme one.txt", os.path.join(tmp, "g.txt"))
check("ftp download", open(os.path.join(tmp,"g.txt")).read() == "ftp hello\n")
blob = os.urandom(90000); up = os.path.join(tmp, "u.bin"); open(up,"wb").write(blob)
B.write_from(up, "/pub/up load.bin")
check("ftp upload (binary, spaced name)",
      open(os.path.join(root,"pub","up load.bin"),"rb").read() == blob)
B.read_to("/big.bin", os.path.join(tmp,"big2.bin"))
check("ftp large download byte-identical",
      open(os.path.join(tmp,"big2.bin"),"rb").read() == open(os.path.join(root,"big.bin"),"rb").read())
B.mkdir("/newdir"); check("ftp mkdir", os.path.isdir(os.path.join(root,"newdir")))
B.rename("/pub/up load.bin", "/newdir/moved.bin")
check("ftp rename", os.path.isfile(os.path.join(root,"newdir","moved.bin")))
B.rmdir("/newdir"); check("ftp recursive rmdir", not os.path.exists(os.path.join(root,"newdir")))
for label, fn in [("listdir missing", lambda: B.listdir("/nope")),
                  ("download missing", lambda: B.read_to("/nope.txt", os.path.join(tmp,"z"))),
                  ("delete missing", lambda: B.remove("/nope.txt"))]:
    try:
        fn(); check(f"ftp {label} raises", False)
    except Exception as e:
        check(f"ftp {label} raises", True, f"{type(e).__name__}: {str(e)[:40]}")

# LIST fallback path (servers without MLSD)
B.ftp.mlsd = lambda *a, **k: (_ for _ in ()).throw(Exception("no MLSD"))
top2 = {e.name: e for e in B.listdir("/")}
check("ftp LIST fallback parser", set(top2) == {"pub", "big.bin"}, sorted(top2))
check("ftp LIST fallback size/dir", top2["big.bin"].size == 120000 and top2["pub"].is_dir)
B.close()
print("\n" + ("ALL FTP TESTS PASSED" if ok else "*** FAILURES ***"))
