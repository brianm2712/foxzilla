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

# ---------------------------------------------------------------- bug 10
print("\nbug_10_movie_disc_with_extras_filed_as_tv  (v2: 5+ videos -> TV, whatever their sizes)")
MB = 1024 * 1024
# Real MakeMKV rips, sizes in MB. Every one has a feature plus 4+ extras.
MOVIE_DISCS = {
    "Batman Year One": [3140, 427, 295, 324, 336, 49, 51, 59],
    "Batman Under the Red Hood": [3584, 409, 337, 340, 79],
    "Batman Mystery of the Batwoman": [3321, 215, 322, 348, 78],
    "Renfield": [5325, 522, 84, 103, 120, 97, 85, 103, 120, 366, 182, 157, 181, 105, 140, 3],
    "A Knights Tale": [3994, 419, 116, 68, 100, 80, 137, 59, 76,
                       70, 90, 110, 85, 95, 120, 105, 88, 92, 99],
}
for title, mbs in MOVIE_DISCS.items():
    paths = [f"B{i}_t{i:02d}.mkv" for i in range(len(mbs))]
    kind, why = MP.classify(title, paths, {p: m * MB for p, m in zip(paths, mbs)})
    check(f"{title}: movie", kind == "movie", why)

# A season is near-equal episodes; its "play all" title is the sum of the rest,
# so even that stays at about half the video.
episodes = [f"B{i}_t{i:02d}.mkv" for i in range(6)]
kind, why = MP.classify("Some Show", episodes, {p: 700 * MB for p in episodes})
check("equal-sized season stays TV", kind == "tv", why)
play_all = episodes + ["A1_t06.mkv"]
sizes = {p: 700 * MB for p in episodes}
sizes["A1_t06.mkv"] = 6 * 700 * MB
kind, why = MP.classify("Some Show", play_all, sizes)
check("season with a play-all title stays TV", kind == "tv", why)
kind, why = MP.classify("S01", ["Show S01E01.mkv"] * 1 + ["x.mkv"], {"Show S01E01.mkv": 9 * MB, "x.mkv": 1})
check("an episode marker still wins over size", kind == "tv", why)

e = Env()
rip = os.path.join(e.upload, "Renfield")
for i, kb in enumerate(MOVIE_DISCS["Renfield"]):
    mkfile(os.path.join(rip, f"D1_t{i:02d}.mkv"), kb * 10)
e.run()
check("end to end: lands in movies", len(videos_under(e.movies)) == 16 and not videos_under(e.shows),
      f"movies={len(videos_under(e.movies))} shows={len(videos_under(e.shows))}")
check("feature promoted, extras kept",
      os.path.exists(os.path.join(e.movies, "Renfield", "Renfield.mkv"))
      and len(os.listdir(os.path.join(e.movies, "Renfield", "extras"))) == 15)
check("handoff completes (source removed)", not os.path.exists(rip), e.tail(1))

# ---------------------------------------------------------------- bug 11
print("\nbug_11_movie_with_extras_fails_reconciliation  (v2: renamed feature looked missing)")
e = Env()
film = os.path.join(e.upload, "Ned Kelly")
for n, kb in [("A1_t00.mkv", 600), ("B1_t01.mkv", 60), ("D2_t02.mkv", 11)]:
    mkfile(os.path.join(film, n), kb * 100)
e.run()
check("reconciles all 3 files", any("reconciled 3/3" in l for l in e.tail(6)), e.tail(3))
check("source removed only after success", not os.path.exists(film))
check("feature and extras in place",
      sorted(videos_under(os.path.join(e.movies, "Ned Kelly"))) ==
      ["Ned Kelly.mkv", os.path.join("extras", "B1_t01.mkv"), os.path.join("extras", "D2_t02.mkv")],
      videos_under(e.movies))

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

# ---------------------------------------------------------------- trigger
print("\ncompletion marker  (an uploader that has verified says so)")
e = Env()
mkfile(os.path.join(e.upload, "Marked.Show.S01E01.mkv"), 2048)
e.run(force=False)                       # no marker: must wait, not act
check("without a marker it waits", os.path.exists(os.path.join(e.upload, "Marked.Show.S01E01.mkv")))
check("logged as watching", any("watching" in l or "in progress" in l for l in e.tail(4)), e.tail(2))

open(MP.marker_for(e.upload, "Marked.Show.S01E01.mkv"), "w").write("done")
e.run(force=False)                       # marker present: act immediately
check("marker bypasses the stability wait",
      not os.path.exists(os.path.join(e.upload, "Marked.Show.S01E01.mkv")))
check("landed in the library", videos_under(e.shows) != [], videos_under(e.shows))
check("said why it acted", any("marked complete by the sender" in l for l in e.tail(12)))
check("marker cleaned up afterwards",
      not os.path.exists(MP.marker_for(e.upload, "Marked.Show.S01E01.mkv")))

print("\nmarkers are not mistaken for uploads, and stale ones are swept")
e = Env()
open(MP.marker_for(e.upload, "Ghost"), "w").write("done")   # item never arrived
e.run(force=False)
check("a lone marker is not processed as an item", videos_under(e.shows) == [])
check("stale marker removed", not os.path.exists(MP.marker_for(e.upload, "Ghost")))

print("\na marker written too early costs a failed run, never data")
e = Env()
mkfile(os.path.join(e.upload, "Early", "a.mkv"), 4096)
open(MP.marker_for(e.upload, "Early"), "w").write("done")
real = MP.reconcile
MP.reconcile = lambda *a, **k: (_ for _ in ()).throw(MP.PickupError("verification failed"))
e.run(force=False)
MP.reconcile = real
check("source survives a premature marker", os.path.exists(os.path.join(e.upload, "Early", "a.mkv")))
check("failure reported", any("FAILED, source left intact" in l for l in e.tail(6)))

# ---------------------------------------------------------------- same-named copies
print("\nsame_named_copies_in_a_pack  (2026-10-26: Toy Story 2 arrived x4, the copies clobbered each other -> '3 missing', retried hourly)")
e = Env()
MP.MIN_FEATURE_BYTES = 10000
pack = os.path.join(e.upload, "Toy.Story.2.1999.REMUX")
full = os.urandom(40000)
mkfile(os.path.join(pack, "Toy.Story.2.1999.REMUX.mkv"), content=full[:35000])       # top-level copy, truncated
for u in ("50E44C52", "AE183644", "D1E39235"):                                      # upload-client duplicate folders
    mkfile(os.path.join(pack, f"{u}-Toy.Story.2.1999.REMUX", "Toy.Story.2.1999.REMUX.mkv"), content=full)
e.run()
all_mkv = [os.path.relpath(os.path.join(dp, f), e.movies)
           for dp, _d, fs in os.walk(e.movies) for f in fs if f.endswith(".mkv")]
feature = os.path.join(e.movies, "Toy.Story.2.1999.REMUX", "Toy.Story.2.1999.REMUX.mkv")
check("pack with four same-named copies hands off",
      not any("FAILED" in l for l in e.tail(12)) and videos_under(e.upload) == [], e.tail(4))
check("the largest copy is the feature", os.path.exists(feature) and os.path.getsize(feature) == 40000)
check("the other copies are kept, not clobbered", len(all_mkv) == 4, all_mkv)
check("duplicates land in _leftovers", sum(1 for p in all_mkv if "_leftovers" in p) == 3, all_mkv)
MP.MIN_FEATURE_BYTES = 200 * 1024 * 1024

# ---------------------------------------------------------------- same-named extras
print("\nsame_named_extras_in_a_movie  (same clobber class on the single-feature path: extras/ flattens names)")
e = Env()
MP.MIN_FEATURE_BYTES = 10000
pack = os.path.join(e.upload, "Warrior (2011)")
mkfile(os.path.join(pack, "Warrior.mkv"), 40000)
mkfile(os.path.join(pack, "Featurettes", "sample.mkv"), 500)
mkfile(os.path.join(pack, "Trailers", "sample.mkv"), 600)
e.run()
extras = sorted(os.listdir(os.path.join(e.movies, "Warrior (2011)", "extras"))) if os.path.isdir(os.path.join(e.movies, "Warrior (2011)", "extras")) else []
check("movie with two same-named extras hands off",
      not any("FAILED" in l for l in e.tail(12)) and videos_under(e.upload) == [], e.tail(4))
check("both extras survive under distinct names", len(extras) == 2, extras)
MP.MIN_FEATURE_BYTES = 200 * 1024 * 1024

# ---------------------------------------------------------------- disc images
print("\ndisc_images_are_held_in_the_inbox  (2026-10-26: DVD .iso rips; Jellyfin cannot play disc images, so filing them hides them)")
e = Env()
mkfile(os.path.join(e.upload, "MY_LEFT_FOOT.iso"), 50000)
mkfile(os.path.join(e.upload, "Bluey", "Bluey S01E01.mkv"), 4096)
e.run()
e.run()
held = [l for l in open(e.log).read().splitlines() if "disc image" in l]
check("the iso stays in the inbox", os.path.exists(os.path.join(e.upload, "MY_LEFT_FOOT.iso")))
check("nothing iso-shaped reaches the libraries",
      not any(f.endswith(".iso") for _dp, _d, fs in os.walk(e.base) if "incoming" not in _dp for f in fs))
check("the hold is logged once, not every tick", len(held) == 1 and "WARN" in held[0], held)
check("other uploads still flow", videos_under(e.shows) != [])

# ---------------------------------------------------------------- disc label
print("\ndisc label -> title")
def mkiso(path, label):
    """17 sectors; a primary volume descriptor at sector 16 carrying the label."""
    pvd = bytearray(2048); pvd[0] = 1; pvd[1:6] = b"CD001"; pvd[7] = 1
    pvd[40:72] = label.encode("ascii").ljust(32)
    mkfile(path, content=bytes(2048 * 16) + bytes(pvd))
e = Env()
mkiso(os.path.join(e.upload, "MY_LEFT_FOOT.iso"), "MY_LEFT_FOOT")
check("label read from the PVD", MP.read_iso_label(os.path.join(e.upload, "MY_LEFT_FOOT.iso")) == "MY_LEFT_FOOT")
for label, want in [("MY_LEFT_FOOT", "My Left Foot"), ("LES_MISERABLES", "Les Miserables"),
                    ("BBCDVD3090", "BBCDVD3090"), ("THE.SNAPPER", "The Snapper"), ("", "")]:
    check(f"title_from_label({label!r}) == {want!r}", MP.title_from_label(label) == want, MP.title_from_label(label))
mkfile(os.path.join(e.upload, "junk.iso"), 4096)
try:
    MP.read_iso_label(os.path.join(e.upload, "junk.iso")); check("non-ISO rejected", False)
except MP.PickupError:
    check("non-ISO rejected", True)

# ---------------------------------------------------------------- year lookup
print("\npicking the year from Jellyfin's metadata search")
R = lambda n, y, p="TheMovieDb": {"Name": n, "ProductionYear": y, "SearchProviderName": p}
check("exact match keeps the provider's accents",
      MP.pick_movie_match([R("Les Misérables", 2012), R("Les Miserables", 1995)], "Les Miserables") == ("Les Misérables", 2012))
check("longer provider title keeps our title, takes the year",
      MP.pick_movie_match([R("My Left Foot: The Story of Christy Brown", 1989), R("My Left Foot", 1990, "OMDb")], "My Left Foot") == ("My Left Foot", 1989))
check("non-TMDb results are ignored", MP.pick_movie_match([R("The Snapper", 1993, "OMDb")], "The Snapper") is None)
check("unrelated results are ignored", MP.pick_movie_match([R("Snapper", 2021)], "The Snapper") is None)
check("no year is no match", MP.pick_movie_match([R("The Snapper", None)], "The Snapper") is None)
check("display name", MP.movie_item_name("My Left Foot", 1989) == "My Left Foot (1989)" and MP.movie_item_name("BBCDVD3090", None) == "BBCDVD3090")
check("slashes cannot escape the folder", MP.movie_item_name("AC/DC: Live", 1992) == "AC-DC Live (1992)", MP.movie_item_name("AC/DC: Live", 1992))

# ---------------------------------------------------------------- ffmpeg wrappers
def ffstubs(base, titles, fail=False):
    """Stand-ins for ffprobe/ffmpeg. titles: {n: seconds, or None for a title that fails}."""
    d = os.path.join(base, "bin"); os.makedirs(d, exist_ok=True)
    probe = os.path.join(d, "ffprobe"); enc = os.path.join(d, "ffmpeg"); calls = os.path.join(d, "ffmpeg.calls")
    with open(probe, "w") as fh:
        fh.write("#!/usr/bin/env python3\nimport sys\nT=%r\nn=int(sys.argv[sys.argv.index('-title')+1])\n"
                 "d=T.get(n)\nif d is None: print('Stream not found', file=sys.stderr); sys.exit(1)\nprint(d)\n" % titles)
    with open(enc, "w") as fh:
        fh.write("#!/usr/bin/env python3\nimport sys,os\nopen(%r,'a').write(' '.join(sys.argv[1:])+'\\n')\n"
                 "if %r: print('bad disc', file=sys.stderr); sys.exit(1)\n"
                 "open(sys.argv[-1],'wb').write(b'MKV'*5000)\n" % (calls, fail))
    for p in (probe, enc): os.chmod(p, 0o755)
    MP.FFPROBE, MP.FFMPEG = probe, enc
    return calls

print("\ntitle scan: longest wins, a dead title in the middle is skipped")
e = Env()
ffstubs(e.base, {1: 25.4, 2: 14.4, 3: None, 4: 31.4, 5: 9081.8, 6: 8.5})
found = MP.probe_titles("x.iso")
check("six titles found despite the gap", sorted(n for n, _ in found) == [1, 2, 4, 5, 6], found)
check("longest chosen", MP.longest_title(found) == (5, 9081.8))
out = os.path.join(e.base, "out.mkv")
MP.remux_title("x.iso", 5, out)
check("remux writes the file", os.path.getsize(out) > 0)
ffstubs(e.base, {1: 10.0}, fail=True)
try:
    MP.remux_title("x.iso", 1, out); check("ffmpeg failure raises", False)
except MP.PickupError as err:
    check("ffmpeg failure raises with stderr", "bad disc" in str(err), err)
MP.FFMPEG, MP.FFPROBE = "/nonexistent/ffmpeg", "/nonexistent/ffprobe"

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:"); [print("  - " + f) for f in FAIL]
sys.exit(1 if FAIL else 0)
