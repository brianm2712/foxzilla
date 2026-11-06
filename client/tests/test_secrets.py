#!/usr/bin/env python3
"""
Secrets go to the macOS Keychain when there is one, to secrets.json (0600)
when there isn't, and never into sites.json or a process's arguments.

Driven against a stand-in `security` that keeps its items in a JSON file and
logs every argv it was given, so the test can prove the secret was passed on
stdin. Set FOXZILLA_TEST_KEYCHAIN=1 to also round-trip one throwaway item
through the real login keychain (service 'foxzilla-selftest').
"""
import json, os, stat, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import foxzilla as F
from foxzilla import config as C

ok = True
def check(l, c, e=""):
    global ok
    print(("  PASS " if c else "  FAIL ") + l + (f"   {e}" if e else "")); ok = ok and bool(c)

cfg = tempfile.mkdtemp()
F.CONFIG_DIR = cfg
F.SITES_FILE = os.path.join(cfg, "sites.json")
F.SECRETS_FILE = os.path.join(cfg, "secrets.json")

fakedir = tempfile.mkdtemp()
DB, ARGV = os.path.join(fakedir, "db.json"), os.path.join(fakedir, "argv.log")
fake = os.path.join(fakedir, "security")
with open(fake, "w") as fh:
    fh.write(f'''#!{sys.executable}
import json, os, shlex, sys
DB, ARGV = {DB!r}, {ARGV!r}
open(ARGV, "a").write(json.dumps(sys.argv[1:]) + "\\n")
db = json.load(open(DB)) if os.path.exists(DB) else {{}}
def opt(args, flag):
    return args[args.index(flag) + 1] if flag in args else None
def run(args):
    cmd = args[0]
    key = f"{{opt(args, '-s')}}/{{opt(args, '-a')}}"
    if cmd == "add-generic-password":
        if key in db and "-U" not in args:
            return 45
        db[key] = opt(args, "-w"); return 0
    if cmd == "find-generic-password":
        if key not in db:
            print("security: The specified item could not be found in the keychain.",
                  file=sys.stderr); return 44
        print(db[key]); return 0
    if cmd == "delete-generic-password":
        return 0 if db.pop(key, None) is not None else 44
    return 1
if sys.argv[1:2] == ["-i"]:
    rc = 0
    for line in sys.stdin:
        if line.strip():
            rc = run(shlex.split(line)) or rc
else:
    rc = run(sys.argv[1:])
json.dump(db, open(DB, "w"))
sys.exit(rc)
''')
os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)

def argv_log():
    return open(ARGV).read() if os.path.exists(ARGV) else ""

SECRET = 'p@ss w"rd \\ $HOME `x`'

print("\nwith a keychain")
C.SECURITY_BIN, C.USE_KEYCHAIN = fake, True
where = F.set_secret("mediadrop", SECRET)
check("stored in the keychain", where == "keychain", where)
check("read back exactly", F.get_secret("mediadrop") == SECRET, F.get_secret("mediadrop"))
check("the secret never appeared in an argument list", "p@ss" not in argv_log(), argv_log())
check("nothing written to secrets.json", not os.path.exists(F.SECRETS_FILE))
F.set_secret("mediadrop", "second")
check("updating replaces it", F.get_secret("mediadrop") == "second")
F.delete_secret("mediadrop")
check("delete removes it", F.get_secret("mediadrop") == "")
check("a missing secret is ''", F.get_secret("never-saved") == "")

print("\nan old secrets.json entry is still read, and moved on next save")
json.dump({"oldsite": "from-file"}, open(F.SECRETS_FILE, "w"))
check("read from the file when the keychain has nothing", F.get_secret("oldsite") == "from-file")
F.set_secret("oldsite", "from-file")
check("saving moves it into the keychain", json.load(open(F.SECRETS_FILE)) == {})

print("\nwhen the keychain refuses, the file is the fallback")
C.SECURITY_BIN = "/bin/false"
where = F.set_secret("fallback", "in-the-file")
check("stored in the file", where == "file", where)
check("at mode 0600", stat.S_IMODE(os.stat(F.SECRETS_FILE).st_mode) == 0o600,
      oct(os.stat(F.SECRETS_FILE).st_mode))
check("and read back", F.get_secret("fallback") == "in-the-file")

print("\nwithout a keychain (Linux)")
C.USE_KEYCHAIN = False
where = F.set_secret("linuxsite", "pw")
check("file storage", where == "file" and json.load(open(F.SECRETS_FILE))["linuxsite"] == "pw")
check("save_secret is the same call (it used to be dead code)", F.save_secret is F.set_secret)
C.SECURITY_BIN, C.USE_KEYCHAIN = fake, True

print("\nS3 keys leave sites.json")
sites = [{"name": "backups", "type": "s3", "bucket": "b", "region": "eu-west-1",
          "access_key": "AKIAEXAMPLE", "secret_key": "s3cr3t/key"},
         {"name": "server", "type": "sftp", "host": "myserver"}]
F.save_sites(sites)
changed = F.move_site_secrets(sites)
check("reports that it moved something", changed)
F.save_sites(sites)
raw = open(F.SITES_FILE).read()
check("no S3 key left in sites.json", "s3cr3t" not in raw and "AKIAEXAMPLE" not in raw, raw)
check("the rest of the site is untouched", sites[0]["bucket"] == "b" and sites[1] == {
      "name": "server", "type": "sftp", "host": "myserver"})
check("a second pass has nothing to move", not F.move_site_secrets(sites))
b = F.make_backend(sites[0])
check("the backend still gets both keys", (b.access_key, b.secret_key) == ("AKIAEXAMPLE", "s3cr3t/key"),
      (b.access_key, b.secret_key))
os.environ["AWS_SECRET_ACCESS_KEY"] = "from-env"
b = F.make_backend({"name": "envonly", "type": "s3", "bucket": "b"})
check("the environment still works when nothing is stored", b.secret_key == "from-env")
del os.environ["AWS_SECRET_ACCESS_KEY"]

print("\nrenaming a site takes its secrets along")
F.set_secret("backups", "a-password")
F.rename_site_secrets("backups", "offsite")
check("password moved", F.get_secret("offsite") == "a-password" and F.get_secret("backups") == "")
check("S3 key moved", F.get_secret(F.secret_key_for("offsite", "secret_key")) == "s3cr3t/key")

if os.environ.get("FOXZILLA_TEST_KEYCHAIN") == "1" and os.path.exists("/usr/bin/security"):
    print("\nthe real login keychain")
    C.SECURITY_BIN, C.USE_KEYCHAIN, C.KEYCHAIN_SERVICE = "/usr/bin/security", True, "foxzilla-selftest"
    where = F.set_secret("probe", SECRET)
    check("stored", where == "keychain", where)
    check("read back exactly", F.get_secret("probe") == SECRET)
    F.delete_secret("probe")
    check("deleted", F.get_secret("probe") == "")

print(f"\n{'ALL PASS' if ok else '*** FAILURES ***'}")
sys.exit(0 if ok else 1)
