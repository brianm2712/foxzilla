#!/usr/bin/env python3
"""
SFTP transfers can be stopped: cancel kills the sftp process, a transfer
that stops making progress is killed by a watchdog, keepalives are on, and
each backend multiplexes through its own private control-socket directory.

Driven against a stand-in `sftp` (and `ssh`) on PATH, so nothing touches a
real server. The stand-in records its pid so the test can check it is gone.
"""
import os, stat, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import foxzilla as F

ok = True
def check(l, c, e=""):
    global ok
    print(("  PASS " if c else "  FAIL ") + l + (f"   {e}" if e else "")); ok = ok and bool(c)

fakedir = tempfile.mkdtemp()
PIDFILE = os.path.join(fakedir, "sftp.pid")
with open(os.path.join(fakedir, "sftp"), "w") as fh:
    fh.write(f'''#!{sys.executable}
# Batch-mode sftp stand-in. FAKE_SFTP selects how a 'get' behaves:
#   trickle - append a little to the local file every 0.1s, for a minute
#   hang    - write a few bytes, then stop and never finish
#   quick   - write the whole file and exit
import os, re, sys, time
open({PIDFILE!r}, "w").write(str(os.getpid()))
mode = os.environ.get("FAKE_SFTP", "quick")
for line in sys.stdin:
    line = line.strip()
    print("sftp> " + line)
    if "pwd" in line:
        print("Remote working directory: /home/u")
    m = re.match(r'-?get "(.*)" "(.*)"', line)
    if m:
        local = m.group(2)
        with open(local, "wb") as fh:
            if mode == "quick":
                fh.write(b"x" * 1000)
            elif mode == "hang":
                fh.write(b"x" * 10); fh.flush()
                time.sleep(3600)
            else:
                for _ in range(600):
                    fh.write(b"x" * 100); fh.flush(); time.sleep(0.1)
    if line.startswith("-ls -l") or line.startswith("ls -l"):
        print("-rw-r--r--    1 u  u   60000 Sep  5 12:06 /home/u/big.bin")
''')
with open(os.path.join(fakedir, "ssh"), "w") as fh:
    fh.write("#!/bin/sh\nexit 0\n")
for f in ("sftp", "ssh"):
    p = os.path.join(fakedir, f)
    os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
os.environ["PATH"] = fakedir + os.pathsep + os.environ["PATH"]

def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True            # (the backend reaps what it kills, so no zombies)

def last_pid():
    return int(open(PIDFILE).read())

print("\nkeepalives and the control socket")
B = F.SFTPBackend(host="h", user="u")
args = B._base_args()
check("ServerAliveInterval=15", "ServerAliveInterval=15" in args, args)
check("ServerAliveCountMax=4", "ServerAliveCountMax=4" in args)
ctl_dir = os.path.dirname(B._ctl)
check("control socket lives in a private directory",
      stat.S_IMODE(os.stat(ctl_dir).st_mode) == 0o700, oct(os.stat(ctl_dir).st_mode))
check("not the old predictable /tmp/.foxzilla-<uid>-… name", ".foxzilla-" not in B._ctl, B._ctl)
B2 = F.SFTPBackend(host="h", user="u")
check("two backends never share a control directory",
      os.path.dirname(B2._ctl) != ctl_dir)
check("socket path stays short enough for a unix socket", len(B._ctl) < 80, len(B._ctl))
B.close(); B2.close()
check("close removes the directory", not os.path.exists(ctl_dir))
B3 = F.SFTPBackend(host="h", user="u")
B3.close()
B3.connect()                      # reconnecting after close must still work
check("a closed backend can reconnect (directory recreated)",
      B3.home() == "/home/u" and os.path.isdir(os.path.dirname(B3._ctl)))
B3.close()

print("\ncancel kills the transfer")
os.environ["FAKE_SFTP"] = "trickle"
B = F.SFTPBackend(host="h", user="u")
dst = os.path.join(tempfile.mkdtemp(), "big.bin")
calls = []
def cancel_soon(done, total, rate=0):
    calls.append(done)
    if len(calls) >= 3:
        raise F.TransferError("cancelled")
t0 = time.monotonic()
try:
    B.read_to("/home/u/big.bin", dst, cancel_soon)
    check("cancel raised", False, "returned normally")
except F.TransferError as e:
    check("cancel raised", "cancel" in str(e), str(e))
took = time.monotonic() - t0
check("and returned promptly", took < 10, f"{took:.1f}s")
time.sleep(0.3)
check("the sftp process is gone", not alive(last_pid()))
size = os.path.getsize(dst) if os.path.exists(dst) else 0
time.sleep(0.6)
size2 = os.path.getsize(dst) if os.path.exists(dst) else 0
check("nothing is still writing the file", size == size2, (size, size2))
B.close()

print("\na stalled transfer is killed by the watchdog")
os.environ["FAKE_SFTP"] = "hang"
B = F.SFTPBackend(host="h", user="u")
B.stall_seconds = 2
dst = os.path.join(tempfile.mkdtemp(), "big.bin")
t0 = time.monotonic()
try:
    B.read_to("/home/u/big.bin", dst, lambda *a: None)
    check("stall raised", False, "returned normally")
except F.TransferError as e:
    check("stall raised", "stall" in str(e), str(e))
took = time.monotonic() - t0
check("after about stall_seconds", 1.5 < took < 10, f"{took:.1f}s")
time.sleep(0.3)
check("the hung sftp process is gone", not alive(last_pid()))
B.close()

print("\nthe watchdog leaves a healthy transfer alone")
os.environ["FAKE_SFTP"] = "trickle"
B = F.SFTPBackend(host="h", user="u")
B.stall_seconds = 1
dst = os.path.join(tempfile.mkdtemp(), "big.bin")
seen = []
def stop_after_3s(done, total, rate=0):
    seen.append(done)
    if time.monotonic() - t0 > 3:
        raise F.TransferError("cancelled")
t0 = time.monotonic()
try:
    B.read_to("/home/u/big.bin", dst, stop_after_3s)
except F.TransferError as e:
    check("ran past stall_seconds because bytes kept arriving", "cancel" in str(e), str(e))
check("progress grew", seen and seen[-1] > seen[0], seen[:3] + seen[-2:])
B.close()

print("\na normal transfer still completes")
os.environ["FAKE_SFTP"] = "quick"
B = F.SFTPBackend(host="h", user="u")
dst = os.path.join(tempfile.mkdtemp(), "big.bin")
B.read_to("/home/u/big.bin", dst, lambda *a: None)
check("file written", os.path.getsize(dst) == 1000, os.path.getsize(dst))
B.close()

print(f"\n{'ALL PASS' if ok else '*** FAILURES ***'}")
sys.exit(0 if ok else 1)
