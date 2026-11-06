#!/usr/bin/env python3
"""
The window, with no network: it builds, connects a local pane, and quits.

Skips when this python has no Tkinter (Homebrew's often doesn't; the
python.org build does):
    /Library/Frameworks/Python.framework/Versions/Current/bin/python3 client/tests/test_gui_offline.py
"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    import tkinter as tk
    _probe = tk.Tk()
    _probe.destroy()
except Exception as e:                                       # noqa: BLE001
    print(f"skipped: no usable Tk here ({type(e).__name__})")
    raise SystemExit(0)
import foxzilla as F

ok = True
def check(l, c, e=""):
    global ok
    print(("  PASS " if c else "  FAIL ") + l + (f"   {e}" if e else "")); ok = ok and bool(c)

cfg = tempfile.mkdtemp()
F.CONFIG_DIR = cfg
F.SITES_FILE = os.path.join(cfg, "sites.json")
F.SECRETS_FILE = os.path.join(cfg, "secrets.json")
work = tempfile.mkdtemp()
open(os.path.join(work, "hello.txt"), "w").write("hi\n")
os.makedirs(os.path.join(work, "Show", "S01"))
for n in ("Show/a.mkv", "Show/S01/b.mkv"):
    open(os.path.join(work, n), "w").write(n)
drop = tempfile.mkdtemp()
F.save_sites([{"name": "Local", "type": "local", "path": work},
              {"name": "Drop", "type": "local", "path": drop, "drop": True}])

root = tk.Tk(); root.withdraw()
app = F.App(root)

def settle(ms=1000):
    # Run the real event loop for a bounded time. (root.update() in a loop
    # can block forever on macOS's Tk with a withdrawn window.)
    root.after(ms, root.quit)
    root.mainloop()

settle()
check("left pane connected to the local site", app.left.backend is not None
      and app.left.path == work, app.left.path)
check("left pane listed the folder",
      sorted(e.name for e in app.left.entries) == ["Show", "hello.txt"],
      [e.name for e in app.left.entries])
check("the queue view starts empty", not app.qtree.get_children())
check("log has the connect line", "connected" in app.logbox.get("1.0", "end"))

print("\nsending to a drop folder marks each item complete")
import shutil
app.right.site_var.set("Drop"); app.right.connect(); settle(800)
check("right pane on the drop site", app.right.path == drop, app.right.path)
for scan in (True, False):
    for f in os.listdir(drop):
        p = os.path.join(drop, f)
        shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
    app.scan_var.set(scan)
    app.left.tree.selection_set(("hello.txt", "Show"))
    app.transfer(app.left)
    settle(2500)
    got = sorted(os.listdir(drop))
    check(f"scan={scan}: both items and a marker for each",
          got == [".Show.complete", ".hello.txt.complete", "Show", "hello.txt"], got)
    check(f"scan={scan}: the folder arrived whole",
          os.path.exists(os.path.join(drop, "Show", "S01", "b.mkv")))

print("\nthe queue has its own connection to a remote site")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import json, ftpmock
ftp_root = tempfile.mkdtemp()
fport = ftpmock.serve(ftp_root)
app.sites.append({"name": "FTP", "type": "ftp", "host": "127.0.0.1", "port": fport,
                  "user": ftpmock.USER})
json.dump({"FTP": ftpmock.PASSWORD}, open(F.SECRETS_FILE, "w"))
app.right.reload_sites("FTP"); app.right.connect(); settle(800)
check("right pane on FTP", app.right.backend is not None and app.right.backend.scheme == "ftp")
app.scan_var.set(False)
app.left.tree.selection_set(("hello.txt",))
app.transfer(app.left)
settle(1500)
sent = [j for j in app.queue.jobs if j.name == "hello.txt" and j.dst.scheme == "ftp"]
check("file arrived over FTP", os.path.exists(os.path.join(ftp_root, "hello.txt")))
check("through a backend that is not the pane's", sent and sent[-1].dst is not app.right.backend,
      sent)
check("and the pane can still browse", "hello.txt" in [e.name for e in app.right.backend.listdir("/")])

print("\nquit asks first while transfers are running")
import time
from foxzilla import gui as G

class Slow(F.Backend):
    is_local = False
    def size_of(self, path): return 100
    def read_to(self, path, local, progress=None):
        for i in range(200):
            progress and progress(i, 200, 0)
            time.sleep(0.05)
        open(local, "wb").write(b"x")

job = F.Job(src=Slow(), src_path="/r/slow.bin", dst=F.LocalBackend(),
            dst_path=os.path.join(tempfile.mkdtemp(), "slow.bin"))
app.queue.add(job)
t0 = time.time()
while job.state != "running" and time.time() - t0 < 5:
    time.sleep(0.02)
asked = []
G.messagebox.askyesno = lambda *a, **k: (asked.append(a), False)[1]
app.quit()
check("asked before quitting", len(asked) == 1, asked)
check("saying no keeps the window and the transfer", root.winfo_exists()
      and job.state == "running", job.state)
G.messagebox.askyesno = lambda *a, **k: True
app.quit()
check("saying yes cancels the transfer", job.state == "cancelled", job.state)
try:
    root.winfo_exists(); gone = False
except tk.TclError:
    gone = True
check("and closes the window", gone)

print(f"\n{'ALL PASS' if ok else '*** FAILURES ***'}")
sys.exit(0 if ok else 1)
