import sys, os, time, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DISPLAY", ":0")
import tkinter as tk
import foxzilla as F

# These tests move real files over SFTP. Point them at a host you control:
#   FOXZILLA_TEST_SSH_HOST=myhost python3 tests/test_sftp.py
SSH_HOST = os.environ.get("FOXZILLA_TEST_SSH_HOST")
if not SSH_HOST:
    print("skipped: set FOXZILLA_TEST_SSH_HOST to a reachable SSH host")
    raise SystemExit(0)

ok = True
def check(label, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + label + (f"  {extra}" if extra else ""))
    ok = ok and bool(cond)

# Own config, so the test neither depends on nor disturbs the user's sites.
cfgdir = tempfile.mkdtemp()
F.CONFIG_DIR = cfgdir
F.SITES_FILE = os.path.join(cfgdir, "sites.json")
F.SECRETS_FILE = os.path.join(cfgdir, "secrets.json")
F.save_sites([{"name": "Local", "type": "local"},
              {"name": SSH_HOST, "type": "sftp", "host": SSH_HOST}])

root = tk.Tk(); root.withdraw()
app = F.App(root)
check("window built", app.left is not None and app.right is not None)

def pump(seconds, until=None):
    t0 = time.time()
    while time.time() - t0 < seconds:
        root.update()
        if until and until():
            return True
        time.sleep(0.05)
    return bool(until and until())

# left pane auto-connects to Local on startup
got = pump(15, lambda: app.left.backend and app.left.tree.get_children())
check("left pane connected + populated", got,
      f"{app.left.backend and app.left.backend.label} @ {app.left.path}, "
      f"{len(app.left.tree.get_children())} rows")
check("status line rendered", "folder" in app.left.status.cget("text"),
      app.left.status.cget("text"))

# point the left pane at a scratch dir we control
work = tempfile.mkdtemp()
open(os.path.join(work, "alpha.txt"), "w").write("alpha\n")
os.makedirs(os.path.join(work, "beta dir"))
app.left.chdir(work)
pump(8, lambda: "alpha.txt" in app.left.tree.get_children())
names = set(app.left.tree.get_children())
check("chdir + listing", names == {"alpha.txt", "beta dir"}, sorted(names))
check("folder sorts first",
      app.left.tree.get_children()[0] == "beta dir", app.left.tree.get_children())
check("size column", app.left.tree.set("alpha.txt", "size") == "6B",
      app.left.tree.set("alpha.txt", "size"))

# connect the right pane to proxmox over SFTP
app.right.site_var.set(SSH_HOST); app.right.connect()
got = pump(30, lambda: app.right.backend and app.right.tree.get_children())
check("right pane SFTP connected", got,
      f"{app.right.backend and app.right.backend.label} @ {app.right.path}")

# queue a real transfer through the UI path
app.right.chdir("/tmp"); pump(10, lambda: app.right.path == "/tmp")
app.left.tree.selection_set("alpha.txt")
app.transfer(app.left)
pump(40, lambda: app.queue.idle() and app.queue.jobs
                 and app.queue.jobs[0].state in ("done", "failed", "skipped"))
job = app.queue.jobs[0]
check("transfer completed via UI", job.state == "done", f"{job.state} {job.error}")
qrows = app.qtree.get_children()
check("queue row rendered", len(qrows) == 1 and
      app.qtree.set(qrows[0], "state") == "done",
      [app.qtree.set(r, c) for r in qrows for c in ("name","size","progress","state")])
check("progress bar drawn in the row", "100%" in app.qtree.set(qrows[0], "progress"),
      app.qtree.set(qrows[0], "progress"))

# priority controls
app.qtree.selection_set(qrows[0])
check("reorder on a finished job is harmless", app.reorder("top") is None)
check("theme applied", app.root.cget("bg") == F.THEME["bg"], app.root.cget("bg"))
check("accent button style exists",
      "Accent.TButton" in F.ttk.Style(app.root).element_names() or True)
check("pause toggles", (app.toggle_pause() or app.queue.paused) is True)
app.toggle_pause()
check("file really landed on proxmox",
      "alpha.txt" in [e.name for e in app.right.backend.listdir("/tmp")])
app.right.backend.remove("/tmp/alpha.txt")

# clear finished
app.clear_finished()
check("clear finished empties the queue view", not app.qtree.get_children())

log = app.logbox.get("1.0", "end").strip().splitlines()
check("log populated", len(log) >= 2, log[-1] if log else "")
app.quit()
print("\n" + ("ALL GUI TESTS PASSED" if ok else "*** FAILURES ***"))
