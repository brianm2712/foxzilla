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
def flaky(path, hasher=None, on_chunk=None):
    calls["n"] += 1
    return "0" * 64 if calls["n"] == 2 else real_sha(path, hasher, on_chunk)
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

# ---------------------------------------------------------------- disc images
print("\ndisc_images_are_held_when_ffmpeg_is_missing  (the 2026-10-26 behaviour, kept for images without ffmpeg)")
e = Env()
MP.FFMPEG, MP.FFPROBE = "/nonexistent/ffmpeg", "/nonexistent/ffprobe"
mkiso(os.path.join(e.upload, "MY_LEFT_FOOT.iso"), "MY_LEFT_FOOT")
mkfile(os.path.join(e.upload, "Bluey", "Bluey S01E01.mkv"), 4096)
e.run(); e.run()
held = [l for l in open(e.log).read().splitlines() if "disc image" in l]
check("the iso stays in the inbox", os.path.exists(os.path.join(e.upload, "MY_LEFT_FOOT.iso")))
check("nothing iso-shaped reaches the libraries",
      not any(f.endswith(".iso") for _dp, _d, fs in os.walk(e.base) if "incoming" not in _dp for f in fs))
check("the hold is logged once, not every tick", len(held) == 1 and "WARN" in held[0], held)
check("other uploads still flow", videos_under(e.shows) != [])

print("\ndvd_iso_is_remuxed_and_filed  (2026-10-26: the three held DVD rips)")
e = Env()
calls = ffstubs(e.base, {1: 13.0, 2: 5927.5})
MP.jf_movie_lookup = lambda host, creds, title, log: ("My Left Foot", 1989)
mkiso(os.path.join(e.upload, "MY_LEFT_FOOT.iso"), "MY_LEFT_FOOT")
e.run()
today = ".removed-" + time.strftime("%Y%m%d")
park = os.path.join(e.upload, today, "MY_LEFT_FOOT.iso")
check("the longest title was remuxed", "-title 2" in open(calls).read())
check("log names the title and how many were readable", any("title 2 (98.8 min, longest of 2 readable)" in l for l in e.tail(30)), e.tail(30)[:3])
check("iso parked, not deleted", os.path.exists(park) and not os.path.exists(os.path.join(e.upload, "MY_LEFT_FOOT.iso")))
check("the mkv is filed in Movies on the same tick, through the verified handoff",
      videos_under(e.movies) == [os.path.join("My Left Foot (1989)", "My Left Foot (1989).mkv")]
      and any("reconciled 1/1" in l for l in e.tail(12)), videos_under(e.movies))
check("inbox clean apart from the parked iso", sorted(os.listdir(e.upload)) == [today], os.listdir(e.upload))
check("the marker was consumed, not swept", not os.path.exists(MP.marker_for(e.upload, "My Left Foot (1989)")))
check("no 'holding' line", not any("holding" in l for l in open(e.log).read().splitlines()))

print("\na .name file beside the iso wins outright")
e = Env()
ffstubs(e.base, {1: 48.0, 2: 5456.0})
MP.jf_movie_lookup = lambda *a: (_ for _ in ()).throw(AssertionError("lookup must not run"))
mkiso(os.path.join(e.upload, "BBCDVD3090.iso"), "BBCDVD3090")
mkfile(os.path.join(e.upload, "BBCDVD3090.iso.name"), content=b"The Snapper (1993)\n")
e.run()
check("named from the file", videos_under(e.movies) == [os.path.join("The Snapper (1993)", "The Snapper (1993).mkv")], videos_under(e.movies))
check(".name parked with the iso", os.path.exists(os.path.join(e.upload, today, "BBCDVD3090.iso.name")), os.listdir(os.path.join(e.upload, today)))
check("the .name file was never treated as an upload", not any("BBCDVD3090.iso.name" in l for l in e.tail(20)), e.tail(5))

print("\nlookup failure -> label only, warned, not fatal")
e = Env()
ffstubs(e.base, {1: 100.0})
MP.jf_movie_lookup = lambda *a: (_ for _ in ()).throw(OSError("jellyfin down"))
mkiso(os.path.join(e.upload, "LES_MISERABLES.iso"), "LES_MISERABLES")
e.run()
check("filed under the title-cased label", os.path.isdir(os.path.join(e.movies, "Les Miserables")), os.listdir(e.movies))
check("warned about the missing year", any("WARN" in l and "year" in l for l in e.tail(20)), e.tail(5))

print("\nffmpeg failure: iso stays, warned once, not retried every tick")
e = Env()
calls = ffstubs(e.base, {1: 100.0}, fail=True)
MP.jf_movie_lookup = lambda *a: None
mkiso(os.path.join(e.upload, "BAD.iso"), "BAD")
e.run(); e.run(); e.run()
check("iso still in the inbox", os.path.exists(os.path.join(e.upload, "BAD.iso")))
check("ffmpeg ran once", open(calls).read().count("\n") == 1, open(calls).read())
check("failure logged once", sum(1 for l in open(e.log).read().splitlines() if "convert" in l and "FAILED" in l) == 1, e.tail(6))
conv = os.path.join(e.upload, MP.CONVERTING_DIR)
check("no half-written output left", not os.path.exists(conv) or not os.listdir(conv))
mkfile(os.path.join(e.upload, "BAD.iso.name"), content=b"Fixed (2000)\n")
ffstubs(e.base, {1: 100.0})
e.run()
check("a .name file appearing retries the conversion", os.path.isdir(os.path.join(e.movies, "Fixed (2000)")), os.listdir(e.movies))
MP.FFMPEG, MP.FFPROBE = "/nonexistent/ffmpeg", "/nonexistent/ffprobe"

# ---------------------------------------------------------------- progress reporter
print("\nprogress: throttled, atomic, rate/eta from a rolling window")
e = Env()
sp = os.path.join(e.base, "status", "status.json"); os.makedirs(os.path.dirname(sp))
t = [100.0]
p = MP.Progress(sp, clock=lambda: t[0], mono=lambda: t[0])
p.begin("Film", "copying", 1000, files_total=2)
check("begin writes immediately", p.writes == 1 and os.path.exists(sp), p.writes)
t[0] = 100.5; p.advance(100, file="a.mkv")
t[0] = 101.9; p.advance(100)
check("advance inside 2 s does not write", p.writes == 1, p.writes)
t[0] = 102.1; p.advance(0)
check("advance after 2 s writes", p.writes == 2, p.writes)
doc = json.load(open(sp))
check("status is active with done/total", doc["state"] == "active" and doc["active"]["done"] == 200
      and doc["active"]["total"] == 1000 and doc["active"]["file"] == "a.mkv", doc)
check("no rate before 5 s of samples", doc["active"]["rate"] is None and doc["active"]["eta_s"] is None, doc["active"])
t[0] = 106.0; p.set_done(600)
a = json.load(open(sp))["active"]
check("rate over the window", a["rate"] == 100.0 and a["eta_s"] == 4.0, a)
t[0] = 136.0; p.set_done(900)
a = json.load(open(sp))["active"]
check("old samples age out of the 30 s window", a["rate"] == 10.0 and a["eta_s"] == 10.0, a)
t[0] = 140.0; p.set_done(900)
a = json.load(open(sp))["active"]
check("a stalled transfer has no eta", a["eta_s"] is None, a)
t[0] = 140.1; p.file_done()
check("file_done counts", p.doc["active"]["files_done"] == 1)
p.finish("Film", "ok")
doc = json.load(open(sp))
check("finish always writes and clears active", p.writes == 6 and doc["active"] is None
      and doc["last"]["name"] == "Film" and doc["last"]["result"] == "ok", (p.writes, doc))
check("no tmp file left", os.listdir(os.path.dirname(sp)) == ["status.json"], os.listdir(os.path.dirname(sp)))
check("file is world-readable", oct(os.stat(sp).st_mode & 0o777) == "0o644", oct(os.stat(sp).st_mode))
p.queue([{"name": "Next", "bytes": 5, "files": 1, "state": "waiting"}]); p.idle()
doc = json.load(open(sp))
check("idle keeps last and the queue", doc["state"] == "idle" and doc["last"]["name"] == "Film"
      and doc["queue"][0]["name"] == "Next", doc)
p2 = MP.Progress(sp)
check("a new run inherits last from the file", p2.doc["last"]["name"] == "Film", p2.doc)
p2.queue([{"name": "Film2", "state": "waiting"}, {"name": "Other", "state": "waiting"}])
p2.begin("Film2", "hashing", 10)
check("begin drops the active item from the queue", [r["name"] for r in p2.doc["queue"]] == ["Other"], p2.doc["queue"])

print("\nprogress: an unwritable path warns once and never raises")
msgs = []
bad = MP.Progress("/nonexistent-dir/x/status.json", log=lambda m, level="INFO": msgs.append((level, m)))
bad.begin("A", "copying", 10); bad.finish("A", "ok"); bad.idle()
check("one warning, no exception", len(msgs) == 1 and msgs[0][0] == "WARN", msgs)

print("\nprogress: an empty path is a no-op")
nul = MP.Progress("")
nul.begin("A", "copying", 10); nul.advance(5); nul.finish("A", "ok"); nul.idle()
check("no writes", nul.writes == 0)

class Rec:
    """A stand-in Progress that remembers what it was told."""
    def __init__(self): self.phases = []; self.done = 0; self.files = 0; self.total = None; self.files_total = None
    def begin(self, name, phase, total, unit="bytes", files_total=None):
        self.phases.append((name, phase, unit)); self.total = total; self.files_total = files_total
        self.done = 0; self.files = 0
    def advance(self, n, file=None): self.done += n
    def set_done(self, d, file=None): self.done = d
    def file_done(self, file=None): self.files += 1
    def queue(self, rows): pass
    def finish(self, *a, **k): pass
    def idle(self): pass
    def touch(self): self.touches = getattr(self, 'touches', 0) + 1

print("\nprogress: hashing reports every byte and every file")
e = Env()
item = os.path.join(e.upload, "Pack")
mkfile(os.path.join(item, "a.mkv"), size=MP.CHUNK + 10); mkfile(os.path.join(item, "b.mkv"), size=300)
r = Rec()
m = MP.build_manifest(item, lambda *a, **k: None, progress=r)
check("hashing phase begun for the item", r.phases == [("Pack", "hashing", "bytes")], r.phases)
check("hashed bytes == total", r.done == r.total == m["total_bytes"], (r.done, r.total))
check("files counted", r.files == r.files_total == 2, (r.files, r.files_total))

print("\nprogress: staging and commit run to 100%")
e = Env()
item = os.path.join(e.upload, "Pack")
mkfile(os.path.join(item, "a.mkv"), size=MP.CHUNK + 10); mkfile(os.path.join(item, "b.mkv"), size=300)
m = MP.build_manifest(item, lambda *a, **k: None)
r = Rec()
staging = os.path.join(e.shows, ".staging", "t1")
MP.stage(item, m, staging, lambda *a, **k: None, progress=r)
check("stage phase is verifying on one filesystem", r.phases[-1] == ("Pack", "verifying", "bytes"), r.phases)
check("stage reaches total bytes", r.done == r.total == m["total_bytes"], (r.done, r.total))
check("stage counts every file", r.files == r.files_total == 2, (r.files, r.files_total))
r2 = Rec()
r2.begin("Pack", "committing", 2, unit="files", files_total=2)
MP.commit(staging, os.path.join(e.shows, "Pack"), os.path.join(e.shows, ".quarantine"),
          lambda *a, **k: None, progress=r2)
check("commit counts every placed file", r2.files == 2, r2.files)

print("\nprogress: the copy path advances each byte once")
e = Env()
item = os.path.join(e.upload, "Pack")
mkfile(os.path.join(item, "a.mkv"), size=MP.CHUNK + 10); mkfile(os.path.join(item, "b.mkv"), size=300)
m = MP.build_manifest(item, lambda *a, **k: None)
r = Rec()
_orig_sf, _orig_fb = MP.same_filesystem, MP.free_bytes
MP.same_filesystem = lambda a, b: False
MP.free_bytes = lambda p: 10 ** 12
try:
    MP.stage(item, m, os.path.join(e.shows, ".staging", "t2"), lambda *a, **k: None, progress=r)
finally:
    MP.same_filesystem, MP.free_bytes = _orig_sf, _orig_fb
check("copy path phase is copying", r.phases[-1] == ("Pack", "copying", "bytes"), r.phases)
check("copy path reaches total exactly", r.done == r.total == m["total_bytes"], (r.done, r.total))

print("\nprogress: a whole process_item walks hashing -> stage -> committing")
e = Env()
mkfile(os.path.join(e.upload, "Show S01", "Show.S01E01.mkv"), size=500)
mkfile(os.path.join(e.upload, "Show S01", "Show.S01E02.mkv"), size=500)
r = Rec()
cfg = dict(MP.DEFAULTS, upload_dir=e.upload, movies_dest=e.movies, shows_dest=e.shows,
           receipts_dir=e.receipts)
MP.process_item(cfg, "Show S01", lambda *a, **k: None, progress=r)
check("phases in order", [p for _, p, _ in r.phases] == ["hashing", "verifying", "committing"], r.phases)

print("\nprogress: hardening (nothing escapes, monotonic pacing, inert null instance)")
import pathlib
e = Env()
sp = os.path.join(e.base, "hard", "status.json"); os.makedirs(os.path.dirname(sp))
t = [100.0]
p = MP.Progress(sp, clock=lambda: t[0], mono=lambda: t[0])
p.begin("A", "copying", None)
t[0] = 106.0; p.advance(10)
t[0] = 109.0; p.advance(10)
try:
    a = json.load(open(sp))["active"]
    check("unknown total: written, eta_s None", a["done"] == 20 and a["eta_s"] is None, a)
except Exception as ex:
    check("unknown total: written, eta_s None", False, ex)
t[0] = 120.0
try:
    p.advance(10, file=pathlib.Path("x/y.mkv"))
    ok, err = True, None
except Exception as ex:
    ok, err = False, ex
check("Path value does not raise", ok, err)
check("Path value stringified in file", json.load(open(sp))["active"]["file"] == "x/y.mkv")
check("no tmp left after Path write", os.listdir(os.path.dirname(sp)) == ["status.json"], os.listdir(os.path.dirname(sp)))

wd = os.path.join(e.base, "rearm")
wmsgs = []
w = MP.Progress(os.path.join(wd, "status.json"), log=lambda m, level="INFO": wmsgs.append((level, m)))
w.begin("A", "copying", 10)
check("first failure warns", len(wmsgs) == 1, wmsgs)
os.makedirs(wd); w.idle()
check("write succeeds once dir exists", os.path.exists(os.path.join(wd, "status.json")) and len(wmsgs) == 1, wmsgs)
shutil.rmtree(wd); w.idle()
check("warning re-armed after a success", len(wmsgs) == 2 and wmsgs[1][0] == "WARN", wmsgs)

sp2 = os.path.join(e.base, "mono.json")
wall, mono = [1000.0], [100.0]
m = MP.Progress(sp2, clock=lambda: wall[0], mono=lambda: mono[0])
m.begin("A", "copying", 1000)
wall[0] -= 500; mono[0] = 102.5; m.advance(5)
check("wall clock stepping back does not silence progress", m.writes == 2, m.writes)
wall[0] -= 500; mono[0] = 105.0; m.advance(5)
check("and keeps writing", m.writes == 3, m.writes)

MP.NULL_PROGRESS.begin("X", "copying", 1)
check("NULL_PROGRESS is never mutated", MP.NULL_PROGRESS.doc["active"] is None and MP.NULL_PROGRESS.doc["state"] == "idle", MP.NULL_PROGRESS.doc)

sp3 = os.path.join(e.base, "back.json")
t = [10.0]
b = MP.Progress(sp3, clock=lambda: t[0], mono=lambda: t[0])
b.begin("A", "copying", 1000)
t[0] = 16.0; b.set_done(600)
t[0] = 30.0; b.set_done(100)
a = json.load(open(sp3))["active"]
check("done moving backwards never gives a negative rate", a["rate"] is not None and a["rate"] >= 0 and (a["eta_s"] is None or a["eta_s"] >= 0), a)

rows = [{"name": "Q", "state": "waiting"}]
q = MP.Progress(os.path.join(e.base, "q.json")); q.queue(rows); rows[0]["name"] = "changed"
check("queue stores copies", q.doc["queue"][0]["name"] == "Q", q.doc["queue"])

print("\nprogress: remux reads ffmpeg's -progress stream")
e = Env()
ffstubs(e.base, {1: 100.0})
enc = os.path.join(e.base, "bin", "ffmpeg")
with open(enc, "w") as fh:
    fh.write("#!/usr/bin/env python3\nimport sys\n"
             "assert '-progress' in sys.argv and sys.argv[sys.argv.index('-progress')+1] == 'pipe:1'\n"
             "print('out_time_us=1000000'); print('progress=continue')\n"
             "print('out_time_us=N/A')\n"
             "print('out_time_us=3500000'); print('progress=end')\n"
             "print('a warning', file=sys.stderr)\n"
             "open(sys.argv[-1],'wb').write(b'MKV'*10)\n")
os.chmod(enc, 0o755); MP.FFMPEG = enc
seen = []
out = os.path.join(e.base, "out.mkv")
MP.remux_title("x.iso", 1, out, on_time=seen.append)
check("out_time parsed in seconds, N/A skipped", seen == [1.0, 3.5], seen)
check("output still written", os.path.getsize(out) == 30)
MP.FFMPEG, MP.FFPROBE = "/nonexistent/ffmpeg", "/nonexistent/ffprobe"

print("\nprogress: a run writes the queue and ends idle with last")
e = Env()
sp = os.path.join(e.base, "status.json")
mkfile(os.path.join(e.upload, "Uploading", "a.mkv.part"), size=100)
mkfile(os.path.join(e.upload, "Film (2020).mkv"), size=2000)
e.run("--status-file", sp)
doc = json.load(open(sp))
check("run ends idle", doc["state"] == "idle" and doc["active"] is None, doc)
check("last is the filed film", doc["last"]["name"] == "Film (2020).mkv" and doc["last"]["result"] == "ok", doc["last"])
rows = {r["name"]: r for r in doc["queue"]}
check("uploading item queued as uploading", rows.get("Uploading", {}).get("state") == "uploading", doc["queue"])
check("queue row has size", rows["Uploading"]["bytes"] == 100 and rows["Uploading"]["files"] == 1, rows["Uploading"])
check("filed film is not in the queue", "Film (2020).mkv" not in rows, doc["queue"])

print("\nprogress: a failed item shows its error in the queue")
e = Env()
sp = os.path.join(e.base, "status.json")
real_pi = MP.process_item
MP.process_item = lambda *a, **k: (_ for _ in ()).throw(MP.PickupError("boom"))
try:
    mkfile(os.path.join(e.upload, "Broken (2001).mkv"), size=100)
    e.run("--status-file", sp)
finally:
    MP.process_item = real_pi
doc = json.load(open(sp))
rows = {r["name"]: r for r in doc["queue"]}
check("failed row with error", rows.get("Broken (2001).mkv", {}).get("state") == "failed"
      and rows["Broken (2001).mkv"].get("error") == "boom", doc["queue"])
check("last records the failure", doc["last"]["result"] == "failed" and doc["last"]["error"] == "boom", doc["last"])

print("\nprogress: a tick that cannot take the lock leaves the status alone")
e = Env()
sp = os.path.join(e.base, "status.json")
open(sp, "w").write('{"sentinel": true}')
held = MP.acquire_lock(e.lock)
check("the test really holds the lock", held is not None and MP.acquire_lock(e.lock) is None)
mkfile(os.path.join(e.upload, "Film (2020).mkv"), size=2000)
e.run("--status-file", sp)
held.close()
check("status untouched", json.load(open(sp)) == {"sentinel": True})
check("the locked-out tick filed nothing", os.path.exists(os.path.join(e.upload, "Film (2020).mkv")))

print("\nprogress: an empty inbox still writes idle")
e = Env()
sp = os.path.join(e.base, "status.json")
e.run("--status-file", sp)
d = json.load(open(sp))
check("idle written with empty queue", d["state"] == "idle" and d["queue"] == [])

print("\nprogress: an iso converted and filed in one tick")
e = Env()
sp = os.path.join(e.base, "status.json")
ffstubs(e.base, {1: 100.0})
MP.jf_movie_lookup = lambda *a: None
mkiso(os.path.join(e.upload, "TEST_DISC.iso"), "TEST_DISC")
e.run("--status-file", sp)
MP.FFMPEG, MP.FFPROBE = "/nonexistent/ffmpeg", "/nonexistent/ffprobe"
doc = json.load(open(sp))
check("the mkv was filed", os.path.isdir(os.path.join(e.movies, "Test Disc")), os.listdir(e.movies))
check("last is the filed mkv item", doc["last"]["name"] == "Test Disc" and doc["last"]["result"] == "ok", doc["last"])
check("no iso or mkv row left in the queue", doc["queue"] == [], doc["queue"])
check("ends idle", doc["state"] == "idle" and doc["active"] is None, doc)

print("\nreview fixes: dry run publishes no status")
e = Env()
sp = os.path.join(e.base, "status.json")
mkfile(os.path.join(e.upload, "Film (2020).mkv"), size=500)
e.run("--status-file", sp, "--dry-run")
check("dry run wrote no status file", not os.path.exists(sp))

print("\nreview fixes: ffmpeg is reaped when the progress callback raises")
import time
e = Env()
ffstubs(e.base, {1: 100.0})
enc = os.path.join(e.base, "bin", "ffmpeg")
with open(enc, "w") as fh:
    fh.write("#!/usr/bin/env python3\nimport time,sys\n"
             "print('out_time_us=1000000', flush=True)\ntime.sleep(30)\n")
os.chmod(enc, 0o755); MP.FFMPEG = enc
def boom(_s): raise RuntimeError("boom")
t0 = time.monotonic(); raised = None
try:
    MP.remux_title("x.iso", 1, os.path.join(e.base, "o.mkv"), on_time=boom)
except RuntimeError as ex:
    raised = ex
dt = time.monotonic() - t0
check("RuntimeError propagates", raised is not None and str(raised) == "boom", raised)
check("returned without waiting for ffmpeg", dt < 10, dt)
MP.FFMPEG, MP.FFPROBE = "/nonexistent/ffmpeg", "/nonexistent/ffprobe"

print("\nreview fixes: Progress.touch keeps the card fresh")
e = Env()
sp = os.path.join(e.base, "touch.json")
t = [100.0]
p = MP.Progress(sp, clock=lambda: t[0], mono=lambda: t[0])
p.begin("A", "committing", None)
w0, u0 = p.writes, json.load(open(sp))["updated"]
t[0] = 103.0; p.touch()
check("touch wrote once", p.writes == w0 + 1, (w0, p.writes))
check("updated moved", json.load(open(sp))["updated"] != u0)
p.touch()
check("touch is throttled", p.writes == w0 + 1, p.writes)
ip = MP.Progress(""); ip.touch()
check("inert touch is a no-op", ip.writes == 0)

print("\nreview fixes: commit merge-hashing touches progress")
e = Env()
stg = os.path.join(e.base, "stg", "Pack"); dst = os.path.join(e.shows, "Pack")
same = os.urandom(MP.CHUNK + 5)
mkfile(os.path.join(stg, "a.mkv"), content=same); mkfile(os.path.join(dst, "a.mkv"), content=same)
r = Rec()
_pl, sk, _cf = MP.commit(stg, dst, os.path.join(e.base, "q"), lambda *a, **k: None, progress=r)
check("identical file skipped", sk == ["a.mkv"], sk)
check("hashing both copies touched the card", getattr(r, "touches", 0) >= 2, getattr(r, "touches", 0))

print("\nempty_items_are_held_not_failed  (live log: 3,230 'no usable files' FAILED lines)")
e = Env()
sp = os.path.join(e.base, "status.json")
os.makedirs(os.path.join(e.upload, "Empty Folder"))
mkfile(os.path.join(e.upload, "Junk Only", ".DS_Store"), 10)
os.makedirs(os.path.join(e.upload, "Marked Empty"))
open(MP.marker_for(e.upload, "Marked Empty"), "w").write("done")
e.run("--status-file", sp); e.run("--status-file", sp); e.run("--status-file", sp)
lines = open(e.log).read().splitlines()
for n in ("Empty Folder", "Junk Only", "Marked Empty"):
    warns = [l for l in lines if f"holding {n}: no usable files" in l]
    check(f"{n}: held with one WARN across 3 runs", len(warns) == 1 and "WARN" in warns[0], warns)
    check(f"{n}: still in the inbox", os.path.isdir(os.path.join(e.upload, n)))
check("no FAILED lines for empty items", not any("FAILED" in l for l in lines), [l for l in lines if "FAILED" in l])
check("a marker does not bypass the hold", os.path.exists(MP.marker_for(e.upload, "Marked Empty")))
rows = {r["name"]: r for r in json.load(open(sp))["queue"]}
check("empty items show 'held: empty' in the queue",
      all(rows.get(n, {}).get("state") == "held: empty" for n in ("Empty Folder", "Junk Only", "Marked Empty")), rows)
mkfile(os.path.join(e.upload, "Empty Folder", "Film.mkv"), 2048)
e.run("--status-file", sp)
check("files appearing in a held empty folder get it filed",
      not os.path.exists(os.path.join(e.upload, "Empty Folder")) and videos_under(e.movies) != [],
      (videos_under(e.movies), e.tail(4)))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:"); [print("  - " + f) for f in FAIL]
sys.exit(1 if FAIL else 0)
