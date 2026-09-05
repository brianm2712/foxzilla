#!/usr/bin/env python3
"""
Regression tests for media_pickup v2.

Every test named bug_* reproduces a failure v1 actually exhibited in
/root/media_pickup_cron.log or left behind in the library.
"""
import json, os, shutil, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "media_pickup.py")
sys.path.insert(0, os.path.dirname(HERE))
import media_pickup as MP

PASS, FAIL = [], []

def check(label, cond, extra=""):
    (PASS if cond else FAIL).append(label)
    print(("  PASS " if cond else "  FAIL ") + label + (f"   {extra}" if extra else ""))

class Env:
    """A throwaway upload dir + two library roots."""
    def __init__(self, split_fs=False):
        self.base = tempfile.mkdtemp()
        self.upload = os.path.join(self.base, "incoming", "upload")
        self.movies = os.path.join(self.base, "media")
        self.shows  = os.path.join(self.base, "pve2", "tv")
        for d in (self.upload, self.movies, self.shows):
            os.makedirs(d)
        self.state = os.path.join(self.base, "state.json")
        self.log   = os.path.join(self.base, "pickup.log")
        self.lock  = os.path.join(self.base, "pickup.lock")
        self.receipts = os.path.join(self.base, "receipts")
    def run(self, *extra, force=True):
        argv = ["--upload-dir", self.upload, "--movies-dest", self.movies,
                "--shows-dest", self.shows, "--state-file", self.state,
                "--log-file", self.log, "--lock-file", self.lock,
                "--receipts-dir", self.receipts, *extra]
        if force:
            argv.append("--force")
        return MP.main(argv)
    def tail(self, n=6):
        try: return open(self.log).read().strip().splitlines()[-n:]
        except OSError: return []

def mkfile(path, size=1024, content=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(content if content is not None else os.urandom(size))

def videos_under(root):
    out = []
    for dp, _dn, fns in os.walk(root):
        for f in fns:
            if os.path.splitext(f)[1].lower() in MP.VIDEO_EXTS:
                out.append(os.path.relpath(os.path.join(dp, f), root))
    return sorted(out)

# ---------------------------------------------------------------- bug 1
print("\nbug_1_multiseason_merge_crash  (v1: double os.rmdir -> half-moved box set)")
e = Env()
show = "BATMAN The Animated Series (1992-1999) COMPLETE S01-S06"
for season in range(1, 7):
    for ep in range(1, 4):
        mkfile(os.path.join(e.upload, show, f"Season {season}", f"S{season:02d}E{ep:02d}.mkv"), 2048)
# destination already exists, which is what drove v1 into merge_into()
os.makedirs(os.path.join(e.shows, show, "Season 1"))
shutil.copyfile(os.path.join(e.upload, show, "Season 1", "S01E01.mkv"),
                os.path.join(e.shows, show, "Season 1", "S01E01.mkv"))
rc = e.run()
dest = os.path.join(e.shows, show)
eps = videos_under(dest)
check("run exits cleanly", rc == 0, f"rc={rc}")
check("all 18 episodes present", len(eps) == 18, f"got {len(eps)}")
check("all six seasons present", len({p.split(os.sep)[0] for p in eps}) == 6)
check("source consumed", not os.path.exists(os.path.join(e.upload, show)))
check("no crash in log", not any("UNEXPECTED" in l or "Traceback" in l for l in e.tail(40)))
check("the already-present episode was deduped, not duplicated",
      any("identical content" in l for l in e.tail(20)))

# ---------------------------------------------------------------- bug 2
print("\nbug_2_retry_duplication  (v1: 'name (1).mkv' on every retry)")
e = Env()
mkfile(os.path.join(e.upload, "Man of Steel (2013)", "Man.of.Steel.2013.mp4"), 4096)
e.run()
first = videos_under(os.path.join(e.movies, "Man of Steel (2013)"))
# the identical upload arrives a second time
committed = os.path.join(e.movies, "Man of Steel (2013)", first[0])
mkfile(os.path.join(e.upload, "Man of Steel (2013)", os.path.basename(first[0])),
       content=open(committed, "rb").read())
e.run()
second = videos_under(os.path.join(e.movies, "Man of Steel (2013)"))
check("no duplicate created", first == second, f"{first} -> {second}")
check("no '(1)' suffixed files anywhere",
      not [p for p in videos_under(e.movies) if "(1)" in p], videos_under(e.movies))
check("identical content was skipped, not copied",
      any("identical content" in l for l in e.tail(20)))

# ---------------------------------------------------------------- bug 3
print("\nbug_3_name_clash_different_content  (must quarantine, never silently keep both)")
e = Env()
mkfile(os.path.join(e.shows, "Show", "S01E01.mkv"), content=b"ORIGINAL" * 100)
mkfile(os.path.join(e.upload, "Show", "S01E01.mkv"), content=b"DIFFERENT" * 100)
e.run()
check("original left untouched",
      open(os.path.join(e.shows, "Show", "S01E01.mkv"), "rb").read().startswith(b"ORIGINAL"))
q = os.path.join(e.shows, ".quarantine")
check("clashing file quarantined", os.path.isdir(q) and videos_under(q), videos_under(q) if os.path.isdir(q) else "no quarantine")

# ---------------------------------------------------------------- bug 4
print("\nbug_4_multimovie_pack_rmtree  (v1: shutil.rmtree deleted everything small)")
e = Env()
MP.MIN_FEATURE_BYTES = 10000          # real features are GB-sized; scale for the test
pack = os.path.join(e.upload, "Star Wars Trilogy")
mkfile(os.path.join(pack, "A New Hope.mkv"), 40000)
mkfile(os.path.join(pack, "Empire Strikes Back.mkv"), 40000)
mkfile(os.path.join(pack, "Return of the Jedi.mkv"), 40000)
mkfile(os.path.join(pack, "behind the scenes.mkv"), 500)      # v1 deleted this
mkfile(os.path.join(pack, "poster.jpg"), 200)                 # and this
e.run()
all_files = [os.path.relpath(os.path.join(dp, f), e.movies)
             for dp, _d, fs in os.walk(e.movies) for f in fs]
check("three features split out",
      sum(1 for p in all_files if p.count(os.sep) == 1 and p.endswith(".mkv")) >= 3)
check("small extra NOT deleted", any("behind the scenes" in p for p in all_files), all_files)
check("artwork NOT deleted", any("poster.jpg" in p for p in all_files))
check("unclaimed files kept in _leftovers", any("_leftovers" in p for p in all_files), all_files)
MP.MIN_FEATURE_BYTES = 200 * 1024 * 1024

# ---------------------------------------------------------------- bug 5
print("\nbug_5_dvd_rip_misclassified  (v1: no episode markers -> movie path -> deleted)")
e = Env()
rip = os.path.join(e.upload, "Grabbers Series 1")
for i, name in enumerate(["B1_t00.mkv", "B2_t01.mkv", "B3_t02.mkv",
                          "B4_t03.mkv", "B5_t04.mkv", "B6_t05.mkv"]):
    mkfile(os.path.join(rip, name), 30000 if i else 90000)
e.run()
in_shows = videos_under(e.shows)
in_movies = videos_under(e.movies)
check("classified as TV, not movie", len(in_shows) == 6, f"shows={len(in_shows)} movies={len(in_movies)}")
check("no episode lost", len(in_shows) + len(in_movies) == 6)

# ---------------------------------------------------------------- bug 6
print("\nbug_6_overlapping_runs  (v1: no lock, cron every 5 min raced itself)")
e = Env()
mkfile(os.path.join(e.upload, "Thing", "a.mkv"), 1024)
held = MP.acquire_lock(e.lock)
rc = e.run()
check("second run backs off while lock held", rc == 0 and
      any("another run is still working" in l for l in e.tail(3)), e.tail(2))
check("item untouched by the blocked run", os.path.exists(os.path.join(e.upload, "Thing", "a.mkv")))
held.close()
e.run()
check("processes normally once lock is free", videos_under(e.movies) != [])

# ---------------------------------------------------------------- bug 7
print("\nbug_7_partial_upload  (must not grab a transfer still in flight)")
e = Env()
mkfile(os.path.join(e.upload, "InFlight", "movie.mkv"), 2048)
mkfile(os.path.join(e.upload, "InFlight", "movie.mkv.filepart"), 512)
e.run()
check("partial transfer skipped", os.path.exists(os.path.join(e.upload, "InFlight", "movie.mkv")))
check("nothing committed", videos_under(e.movies) == [], videos_under(e.movies))
check("logged as in progress", any("still in progress" in l for l in e.tail(5)))

# ---------------------------------------------------------------- bug 8
print("\nbug_8_failure_leaves_source_intact")
e = Env()
mkfile(os.path.join(e.upload, "Fragile", "film.mkv"), 4096)
real_reconcile = MP.reconcile
MP.reconcile = lambda *a, **k: (_ for _ in ()).throw(MP.PickupError("simulated verification failure"))
e.run()
MP.reconcile = real_reconcile
check("source still present after failure", os.path.exists(os.path.join(e.upload, "Fragile", "film.mkv")))
check("failure logged as such", any("FAILED, source left intact" in l for l in e.tail(6)))
e.run()
check("retry succeeds after the fault clears", videos_under(e.movies) != [])

# ---------------------------------------------------------------- bug 9
print("\nbug_9_corruption_detected_in_staging")
e = Env()
mkfile(os.path.join(e.upload, "Bitrot", "film.mkv"), 8192)
real_sha = MP.sha256_file
calls = {"n": 0}
def flaky(path, hasher=None):
    calls["n"] += 1
    return "0" * 64 if calls["n"] == 2 else real_sha(path, hasher)
MP.sha256_file = flaky
e.run()
MP.sha256_file = real_sha
check("hash mismatch aborts the handoff", os.path.exists(os.path.join(e.upload, "Bitrot", "film.mkv")))
check("mismatch reported", any("verification failed" in l or "FAILED" in l for l in e.tail(6)))

# ---------------------------------------------------------------- extras
print("\nreceipts, dry-run and single files")
e = Env()
mkfile(os.path.join(e.upload, "Loose.Show.S02E05.1080p.mkv"), 2048)
e.run("--dry-run")
check("dry-run commits nothing", os.path.exists(os.path.join(e.upload, "Loose.Show.S02E05.1080p.mkv")))
e.run()
check("loose episode foldered into a show", os.path.isdir(os.path.join(e.shows, "Loose Show")),
      os.listdir(e.shows))
receipts = os.listdir(e.receipts) if os.path.isdir(e.receipts) else []
check("receipt written", len(receipts) == 1, receipts)
if receipts:
    r = json.load(open(os.path.join(e.receipts, receipts[0])))
    check("receipt records the manifest", r["files"] == 1 and r["kind"] == "tv" and r["manifest"])

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:"); [print("  - " + f) for f in FAIL]
sys.exit(1 if FAIL else 0)
