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
F.save_sites([{"name": "Local", "type": "local", "path": work},
              {"name": "Scratch", "type": "local", "path": tempfile.mkdtemp()}])

root = tk.Tk(); root.withdraw()
app = F.App(root)

def settle(rounds=40):
    for _ in range(rounds):
        root.update()
        root.after(20)

settle()
check("left pane connected to the local site", app.left.backend is not None
      and app.left.path == work, app.left.path)
check("left pane listed the folder", [e.name for e in app.left.entries] == ["hello.txt"],
      [e.name for e in app.left.entries])
check("the queue view starts empty", not app.qtree.get_children())
check("log has the connect line", "connected" in app.logbox.get("1.0", "end"))

app.quit()
print(f"\n{'ALL PASS' if ok else '*** FAILURES ***'}")
sys.exit(0 if ok else 1)
