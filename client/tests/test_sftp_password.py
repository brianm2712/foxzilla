#!/usr/bin/env python3
"""
SFTP password authentication.

Driven against a stand-in `sftp` that prompts exactly like the real one, so
the pty handling, prompt detection and failure path are all exercised without
throwing bad passwords at a live server (which fail2ban would rightly punish).
"""
import os, stat, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import foxzilla as F

ok = True
def check(l, c, e=""):
    global ok
    print(("  PASS " if c else "  FAIL ") + l + (f"   {e}" if e else "")); ok = ok and bool(c)

SECRET = "correct-horse-battery"

fakedir = tempfile.mkdtemp()
fake = os.path.join(fakedir, "sftp")
with open(fake, "w") as fh:
    fh.write(f'''#!/bin/sh
# Stand-in for OpenSSH sftp: prompt on the tty, then run the batch file.
batch=""
while [ $# -gt 0 ]; do
    case "$1" in
        -b) batch="$2"; shift 2 ;;
        *) shift ;;
    esac
done
printf "mediadrop@192.168.68.54's password: "
read -r pw
echo ""
if [ "$pw" = "{SECRET}" ]; then
    while IFS= read -r line; do
        echo "sftp> $line"
        case "$line" in
            *pwd*) echo "Remote working directory: /" ;;
            *"ls -la"*) echo "drwxrwxr-x    ? mediadrop mediadrop     4096 Sep  5 12:06 /upload" ;;
        esac
    done < "$batch"
    exit 0
else
    echo "Permission denied, please try again."
    exit 255
fi
''')
os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
os.environ["PATH"] = fakedir + os.pathsep + os.environ["PATH"]

print("\ncorrect password")
B = F.SFTPBackend(host="192.168.68.54", user="mediadrop", password=SECRET)
B.connect()
check("connects and reads the working directory", B.home() == "/", B.home())
entries = B.listdir("/")
check("listing parses through the pty", [e.name for e in entries] == ["upload"],
      [e.name for e in entries])
check("directory flag survives", entries and entries[0].is_dir)

print("\nwrong password")
bad = F.SFTPBackend(host="192.168.68.54", user="mediadrop", password="nope")
try:
    bad.connect(); check("wrong password rejected", False, "no error raised")
except F.TransferError as e:
    check("wrong password rejected", "authentication failed" in str(e).lower(), str(e)[:60])

print("\nthe password never leaks into the process arguments")
args = F.SFTPBackend(host="h", user="u", password=SECRET)._base_args()
check("not in ssh options", SECRET not in " ".join(args), args)
check("BatchMode disabled so a prompt can happen", "BatchMode=no" in args)
check("pubkey auth suppressed for password sites", "PubkeyAuthentication=no" in args)
check("unknown host pinned, changed host still refused",
      "StrictHostKeyChecking=accept-new" in args)

print("\nkey-auth sites are unaffected")
k = F.SFTPBackend(host="h")._base_args()
check("BatchMode stays on without a password", "BatchMode=yes" in k)
check("no password options added", "PubkeyAuthentication=no" not in k)

print("\nshell-dependent features are skipped for chrooted accounts")
check("no remote checksum attempted", B.checksum("/upload/x") is None)

print("\nsite plumbing")
site = {"name": "mediadrop", "type": "sftp", "host": "192.168.68.54",
        "user": "mediadrop", "auth": "password"}
check("site is flagged as needing a password", F.needs_password(site) is True)
check("key-auth sftp site is not", F.needs_password({"type": "sftp", "host": "pve"}) is False)
be = F.make_backend(site, "typed-at-the-prompt")
check("password reaches the backend", be.password == "typed-at-the-prompt")
check("and is not stored on a key-auth site",
      F.make_backend({"type": "sftp", "host": "pve", "name": "p"}, "x").password == "")

print("\nstart folder")
deep = {"name": "drop", "type": "sftp", "host": "h", "user": "u",
        "path": "/mnt/incoming/upload"}
b = F.make_backend(deep)
check("a site can pin the folder it opens in", b.home() == "/mnt/incoming/upload",
      b.home())
plain = F.make_backend({"name": "p", "type": "sftp", "host": "h"})
check("without one, the server's own home is used",
      plain.start is None and plain.home() == "/", plain.home())

print("\nhost keys and error messages")
E = F.SFTPBackend(host="server.example", user="me")
check("unknown host is pinned on first use, not refused",
      "StrictHostKeyChecking=accept-new" in E._base_args())
check("a CHANGED host key is explained, not swallowed",
      "CHANGED" in E._explain("REMOTE HOST IDENTIFICATION HAS CHANGED!")
      and "ssh-keygen -R server.example" in E._explain("REMOTE HOST IDENTIFICATION HAS CHANGED!"),
      E._explain("REMOTE HOST IDENTIFICATION HAS CHANGED!")[:70])
check("a bare 'Connection closed' host-key failure is named",
      "host key" in E._explain("Host key verification failed.\nConnection closed"),
      E._explain("Host key verification failed.\nConnection closed"))
check("permission denied suggests the password setting",
      "password" in E._explain("Permission denied (publickey)."),
      E._explain("Permission denied (publickey).")[:70])
check("refused connection names the host",
      "refused" in E._explain("ssh: connect to host x port 22: Connection refused"))
check("unknown text falls through to the last line",
      E._explain("something\nodd happened") == "odd happened")

print(f"\n{'ALL PASS' if ok else '*** FAILURES ***'}")
sys.exit(0 if ok else 1)
