#!/usr/bin/env python3
"""The credential path: tokens are stored, passwords never are."""
import json, os, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import media_pickup as MP

PASS, FAIL = [], []
def check(label, cond, extra=""):
    (PASS if cond else FAIL).append(label)
    print(("  PASS " if cond else "  FAIL ") + label + (f"   {extra}" if extra else ""))

MP.CRED_FILE = os.path.join(tempfile.mkdtemp(), "cfg", "credentials.json")
MP.jf_authenticate = lambda host, user, pw: f"token-for-{user}"
logged = []
log = lambda m, lvl="INFO": logged.append(m)

print("\nlogin stores a token, not a password")
# drive cmd_login without a tty by pre-seeding getpass
import getpass
getpass.getpass = lambda prompt="": "the-real-password"
rc = MP.cmd_login("host:8096", user="testuser")
saved = json.load(open(MP.CRED_FILE))
check("login succeeds", rc == 0)
check("token stored", saved.get("token") == "token-for-testuser", saved)
check("password NOT stored", "password" not in saved, list(saved))
check("file is 0600", oct(os.stat(MP.CRED_FILE).st_mode)[-3:] == "600",
      oct(os.stat(MP.CRED_FILE).st_mode)[-3:])
check("directory is 0700",
      oct(os.stat(os.path.dirname(MP.CRED_FILE)).st_mode)[-3:] == "700")
check("raw password string absent from the file",
      "the-real-password" not in open(MP.CRED_FILE).read())

print("\nwhoami / logout")
check("whoami reports the user", MP.cmd_whoami() == 0)
check("logout removes it", MP.cmd_logout() == 0 and not os.path.exists(MP.CRED_FILE))
check("whoami on empty tells you to log in", MP.cmd_whoami() == 1)

print("\nlegacy password file is migrated then erased")
MP.write_cred_file({"user": "testuser", "password": "legacy-password"})
creds = MP.load_credentials("host:8096", log)
after = json.load(open(MP.CRED_FILE))
check("password exchanged for a token", creds.get("token") == "token-for-testuser", creds)
check("password erased from disk", "password" not in after, list(after))
check("legacy password string gone", "legacy-password" not in open(MP.CRED_FILE).read())
check("migration was logged", any("erased" in m for m in logged), logged)

print("\napi key path")
MP.cmd_login("host:8096", api_key="abc123")
check("api key stored", json.load(open(MP.CRED_FILE)).get("api_key") == "abc123")
check("api key needs no password", "password" not in json.load(open(MP.CRED_FILE)))

print("\nenvironment overrides never touch disk")
MP.cmd_logout()
os.environ["JELLYFIN_TOKEN"] = "env-token"
c = MP.load_credentials("host:8096", log)
check("env token used", c.get("token") == "env-token")
check("nothing written to disk", not os.path.exists(MP.CRED_FILE))
del os.environ["JELLYFIN_TOKEN"]

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
