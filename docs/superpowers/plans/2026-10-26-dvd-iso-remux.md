# DVD ISO Remux Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A held DVD ISO in the inbox becomes `<Title (Year)>/<Title (Year)>.mkv`, filed by the existing handoff, with the ISO parked under `.removed-YYYYMMDD`.

**Architecture:** All code lives in `pickup/media_pickup.py` next to the hold branch it replaces: pure helpers (label, title-casing, match picking), two subprocess wrappers (ffprobe title scan, ffmpeg remux), one orchestrator `convert_disc_image()`, and a small change to `main()`. Tests stay in `pickup/tests/test_pickup.py` in its existing print/check style, with stub `ffprobe`/`ffmpeg` scripts and a monkeypatched Jellyfin lookup.

**Tech Stack:** Python 3.12 stdlib, Debian trixie `ffmpeg` (dvdvideo demuxer), Jellyfin `POST /Items/RemoteSearch/Movie`.

Spec: `docs/superpowers/specs/2026-10-26-dvd-iso-remux-design.md`.

Run tests with: `cd pickup && python3 tests/test_pickup.py` (prints PASS/FAIL per check, exits 1 on any FAIL). Commit after each task from `~/foxzilla`.

---

### Task 1: Label and title helpers

**Files:** Modify `pickup/media_pickup.py` (after `DISC_IMAGE_EXTS`), Test `pickup/tests/test_pickup.py` (append before the final summary).

- [ ] Step 1: failing test

```python
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
```

- [ ] Step 2: run, expect `AttributeError: module 'media_pickup' has no attribute 'read_iso_label'`.
- [ ] Step 3: implement

```python
import subprocess, unicodedata   # add to the import block

ISO_SECTOR = 2048

def read_iso_label(path):
    """Volume id from the ISO9660 primary volume descriptor (sector 16)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(16 * ISO_SECTOR)
            pvd = fh.read(ISO_SECTOR)
    except OSError as e:
        raise PickupError(f"cannot read {os.path.basename(path)}: {e}")
    if len(pvd) < ISO_SECTOR or pvd[1:6] != b"CD001":
        raise PickupError(f"{os.path.basename(path)} is not an ISO9660 image")
    return pvd[40:72].decode("ascii", "replace").strip()

def title_from_label(label):
    """MY_LEFT_FOOT -> My Left Foot. Tokens that are not plain words are kept."""
    words = [w for w in re.split(r"[_.\s]+", label.strip()) if w]
    return " ".join(w.capitalize() if w.isalpha() else w for w in words)
```

- [ ] Step 4: run, all new checks PASS, total still passes. Step 5: `git commit -m "pickup: read a DVD ISO's label and turn it into a title"`.

### Task 2: Picking the Jellyfin match

**Files:** Modify `pickup/media_pickup.py` (after `jf_rescan`), Test as above.

- [ ] Step 1: failing test

```python
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
check("slashes cannot escape the folder", MP.movie_item_name("AC/DC: Live", 1992) == "AC-DC Live (1992)")
```

- [ ] Step 2: run, expect AttributeError on `pick_movie_match`. Step 3: implement

```python
def fold(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)).casefold().strip()

def pick_movie_match(results, title):
    """First TheMovieDb result whose name starts with ours. (name, year) or None."""
    for r in results or []:
        if r.get("SearchProviderName") != "TheMovieDb" or not r.get("ProductionYear"):
            continue
        name = r.get("Name") or ""
        if fold(name) == fold(title):
            return name, int(r["ProductionYear"])
        if fold(name).startswith(fold(title)):
            return title, int(r["ProductionYear"])
    return None

def movie_item_name(title, year):
    safe = re.sub(r"[/\\]", "-", title).replace(":", "").strip(". ")
    return f"{safe} ({year})" if year else safe

def jf_auth_headers(creds):
    if creds.get("api_key"):
        return {"X-Emby-Token": creds["api_key"]}
    if creds.get("token"):
        return jf_headers(creds["token"])
    return None

def jf_movie_lookup(host, creds, title, log):
    """Ask Jellyfin's metadata providers for the film's year. None when unsure."""
    headers = jf_auth_headers(creds)
    if not headers:
        log("no Jellyfin credential - filing the disc without a year", "WARN")
        return None
    req = urllib.request.Request(
        f"http://{host}/Items/RemoteSearch/Movie",
        data=json.dumps({"SearchInfo": {"Name": title}}).encode(),
        headers={**headers, "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return pick_movie_match(json.load(r), title)
```

- [ ] Step 4: run, PASS. Step 5: commit `pickup: pick a film's year from Jellyfin's metadata search`.

### Task 3: Title scan and remux wrappers

**Files:** Modify `pickup/media_pickup.py`, Test as above.

- [ ] Step 1: failing test (stub binaries)

```python
def ffstubs(base, titles, fail=False):
    """Stand-ins for ffprobe/ffmpeg. titles: {n: seconds or None (fails)}."""
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
```

- [ ] Step 2: run, expect AttributeError on `probe_titles`. Step 3: implement

```python
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")
TITLE_PROBE_MISSES = 3      # consecutive unreadable titles that end the scan
TITLE_PROBE_MAX = 99

def ffmpeg_available():
    return bool(shutil.which(FFMPEG) and shutil.which(FFPROBE))

def probe_titles(iso):
    """[(title number, seconds)] for every readable title on the disc."""
    found, misses, n = [], 0, 0
    while misses < TITLE_PROBE_MISSES and n < TITLE_PROBE_MAX:
        n += 1
        p = subprocess.run([FFPROBE, "-v", "error", "-f", "dvdvideo", "-title", str(n), "-i", iso,
                            "-show_entries", "format=duration", "-of", "csv=p=0"],
                           capture_output=True, text=True)
        try:
            secs = float(p.stdout.strip().split(",")[0]) if p.returncode == 0 else None
        except ValueError:
            secs = None
        if secs is None:
            misses += 1
            continue
        misses = 0
        found.append((n, secs))
    return found

def longest_title(found):
    return max(found, key=lambda t: t[1]) if found else None

def remux_title(iso, title_no, out_path):
    """Lossless: main title's video, every audio and subtitle stream, chapters."""
    cmd = [FFMPEG, "-nostdin", "-y", "-v", "error", "-f", "dvdvideo", "-title", str(title_no),
           "-i", iso, "-map", "0", "-c", "copy", "-f", "matroska", out_path]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        tail = " | ".join(l for l in p.stderr.strip().splitlines()[-5:] if l.strip())
        raise PickupError(f"ffmpeg failed (rc {p.returncode}): {tail or 'no output'}")
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise PickupError("ffmpeg produced no output")
```

- [ ] Step 4: run, PASS. Step 5: commit `pickup: ffprobe title scan and lossless ffmpeg remux wrappers`.

### Task 4: convert_disc_image() and the main() branch

**Files:** Modify `pickup/media_pickup.py` (`DEFAULTS`, new function before `main`, the hold branch in `main`), Test as above. Replace the existing "disc_images_are_held_in_the_inbox" test with the block below.

- [ ] Step 1: failing tests

```python
print("\ndisc_images_are_held_when_ffmpeg_is_missing  (the 2026-10-26 behaviour, kept for images without ffmpeg)")
e = Env()
MP.FFMPEG, MP.FFPROBE = "/nonexistent/ffmpeg", "/nonexistent/ffprobe"
mkiso(os.path.join(e.upload, "MY_LEFT_FOOT.iso"), "MY_LEFT_FOOT")
mkfile(os.path.join(e.upload, "Bluey", "Bluey S01E01.mkv"), 4096)
e.run(); e.run()
held = [l for l in open(e.log).read().splitlines() if "disc image" in l]
check("the iso stays in the inbox", os.path.exists(os.path.join(e.upload, "MY_LEFT_FOOT.iso")))
check("the hold is logged once, not every tick", len(held) == 1 and "WARN" in held[0], held)
check("other uploads still flow", videos_under(e.shows) != [])

print("\ndvd_iso_is_remuxed_and_filed  (2026-10-26: the three held DVD rips)")
e = Env()
calls = ffstubs(e.base, {1: 13.0, 2: 5927.5})
MP.jf_movie_lookup = lambda host, creds, title, log: ("My Left Foot", 1989)
mkiso(os.path.join(e.upload, "MY_LEFT_FOOT.iso"), "MY_LEFT_FOOT")
e.run()
item = os.path.join(e.upload, "My Left Foot (1989)", "My Left Foot (1989).mkv")
park = os.path.join(e.upload, ".removed-" + time.strftime("%Y%m%d"), "MY_LEFT_FOOT.iso")
check("mkv item appears in the inbox", os.path.exists(item), os.listdir(e.upload))
check("the longest title was remuxed", "-title 2" in open(calls).read())
check("iso parked, not deleted", os.path.exists(park) and not os.path.exists(os.path.join(e.upload, "MY_LEFT_FOOT.iso")))
check("marker written so the next tick acts at once", os.path.exists(MP.marker_for(e.upload, "My Left Foot (1989)")))
check("nothing in the libraries yet", videos_under(e.movies) == [])
e.run(force=False)
check("next tick files it in Movies", videos_under(e.movies) == [os.path.join("My Left Foot (1989)", "My Left Foot (1989).mkv")], videos_under(e.movies))
check("inbox clean apart from the parked iso", sorted(os.listdir(e.upload)) == [".removed-" + time.strftime("%Y%m%d")], os.listdir(e.upload))
check("no 'holding' line", not any("holding" in l for l in open(e.log).read().splitlines()))

print("\na .name file beside the iso wins outright")
e = Env()
ffstubs(e.base, {1: 48.0, 2: 5456.0})
MP.jf_movie_lookup = lambda *a: (_ for _ in ()).throw(AssertionError("lookup must not run"))
mkiso(os.path.join(e.upload, "BBCDVD3090.iso"), "BBCDVD3090")
mkfile(os.path.join(e.upload, "BBCDVD3090.iso.name"), content=b"The Snapper (1993)\n")
e.run()
check("named from the file", os.path.exists(os.path.join(e.upload, "The Snapper (1993)", "The Snapper (1993).mkv")), os.listdir(e.upload))
check(".name parked with the iso", os.path.exists(os.path.join(e.upload, ".removed-" + time.strftime("%Y%m%d"), "BBCDVD3090.iso.name")))
check("the .name file was never treated as an upload", not any("BBCDVD3090.iso.name" in l and "FAILED" in l for l in e.tail(20)))

print("\nlookup failure -> label only, and the hold is logged, not fatal")
e = Env()
ffstubs(e.base, {1: 100.0})
MP.jf_movie_lookup = lambda *a: (_ for _ in ()).throw(OSError("jellyfin down"))
mkiso(os.path.join(e.upload, "LES_MISERABLES.iso"), "LES_MISERABLES")
e.run()
check("filed under the title-cased label", os.path.isdir(os.path.join(e.upload, "Les Miserables")), os.listdir(e.upload))
check("warned about the missing year", any("WARN" in l and "year" in l for l in e.tail(10)), e.tail(5))

print("\nffmpeg failure: iso stays, warned once, not retried every tick")
e = Env()
calls = ffstubs(e.base, {1: 100.0}, fail=True)
MP.jf_movie_lookup = lambda *a: None
mkiso(os.path.join(e.upload, "BAD.iso"), "BAD")
e.run(); e.run(); e.run()
check("iso still in the inbox", os.path.exists(os.path.join(e.upload, "BAD.iso")))
check("ffmpeg ran once", open(calls).read().count("\n") == 1)
check("failure logged once", sum(1 for l in open(e.log).read().splitlines() if "convert" in l and "FAILED" in l) == 1, e.tail(6))
check("no half-written output left", not os.path.exists(os.path.join(e.upload, MP.CONVERTING_DIR)) or not os.listdir(os.path.join(e.upload, MP.CONVERTING_DIR)))
mkfile(os.path.join(e.upload, "BAD.iso.name"), content=b"Fixed (2000)\n")
ffstubs(e.base, {1: 100.0})
e.run()
check("a .name file appearing retries the conversion", os.path.isdir(os.path.join(e.upload, "Fixed (2000)")), os.listdir(e.upload))
MP.FFMPEG, MP.FFPROBE = "/nonexistent/ffmpeg", "/nonexistent/ffprobe"
```

- [ ] Step 2: run, expect failures (`AttributeError: CONVERTING_DIR`, items missing). Step 3: implement

```python
# in DEFAULTS, after "jellyfin_host":
    "park_dir": "",        # where .removed-YYYYMMDD/ goes; empty = the upload dir

NAME_SUFFIX = ".name"       # "<disc>.iso.name": one line, the item name to use
CONVERTING_DIR = ".converting"
PARK_PREFIX = ".removed-"

def name_file_for(iso_path):
    return iso_path + NAME_SUFFIX

def park_dir_today(cfg):
    base = cfg.get("park_dir") or cfg["upload_dir"]
    return os.path.join(base, PARK_PREFIX + time.strftime("%Y%m%d"))

def park(path, park_root):
    """Rename into the park dir (same filesystem). Never copies, never deletes."""
    os.makedirs(park_root, exist_ok=True)
    dest = os.path.join(park_root, os.path.basename(path))
    n = 1
    while os.path.exists(dest):
        n += 1
        dest = os.path.join(park_root, f"{os.path.basename(path)}.{n}")
    try:
        os.rename(path, dest)
    except OSError as e:
        raise PickupError(f"could not park {os.path.basename(path)} in {park_root}: {e}")
    return dest

def disc_item_name(cfg, iso, log):
    """From the .name file if present, else the label plus Jellyfin's year."""
    try:
        with open(name_file_for(iso)) as fh:
            for line in fh:
                if line.strip():
                    return line.strip(), "from .name file"
    except OSError:
        pass
    title = title_from_label(read_iso_label(iso))
    if not title:
        raise PickupError("disc has no volume label and no .name file")
    try:
        match = jf_movie_lookup(cfg["jellyfin_host"], load_credentials(cfg["jellyfin_host"], log), title, log)
    except Exception as e:
        match = None
        log(f"  could not look up the year for '{title}': {e}", "WARN")
    if not match:
        log(f"  no year found for '{title}' - filing it by label; Jellyfin can identify it", "WARN")
        return movie_item_name(title, None), "label only"
    return movie_item_name(*match), f"label + Jellyfin ({match[0]}, {match[1]})"

def convert_disc_image(cfg, name, log):
    """Remux a held DVD image into an MKV item the handoff will file. Returns the item name."""
    iso = os.path.join(cfg["upload_dir"], name)
    started = time.time()
    park_root = park_dir_today(cfg)
    if not same_filesystem(cfg["upload_dir"], os.path.dirname(park_root.rstrip("/")) or park_root):
        raise PickupError(f"park dir {park_root} is not on the inbox filesystem")
    item, why = disc_item_name(cfg, iso, log)
    final = os.path.join(cfg["upload_dir"], item)
    if os.path.exists(final):
        raise PickupError(f"converted item already in the inbox: {item}")
    found = probe_titles(iso)
    best = longest_title(found)
    if not best:
        raise PickupError("no readable DVD titles on the disc")
    log(f"  title {best[0]} of {len(found)} ({best[1] / 60:.1f} min) -> {item}/{item}.mkv ({why})")
    work = os.path.join(cfg["upload_dir"], CONVERTING_DIR, item)
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    try:
        remux_title(iso, best[0], os.path.join(work, item + ".mkv"))
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise
    os.rename(work, final)
    with open(marker_for(cfg["upload_dir"], item), "w") as fh:
        fh.write("converted from " + name + "\n")
    parked = park(iso, park_root)
    if os.path.exists(name_file_for(iso)):
        park(name_file_for(iso), park_root)
    prune_empty(os.path.join(cfg["upload_dir"], CONVERTING_DIR))
    log(f"  -> converted in {time.time() - started:.0f}s; {name} parked at {parked}")
    return item
```

In `main()`:

```python
        entries = [e for e in sorted(os.listdir(cfg["upload_dir"]))
                   if not e.startswith(".") and not is_junk(e) and not e.endswith(NAME_SUFFIX)]
        ...
            imgs = [p for p in now["shape"] if os.path.splitext(p)[1].lower() in DISC_IMAGE_EXTS]
            convertible = bool(imgs) and os.path.isfile(path) and ffmpeg_available()
            if imgs and not convertible:
                # (existing hold branch, unchanged, with the reason widened)
                why = "ffmpeg is not installed" if os.path.isfile(path) else "put the .iso at the top level of the inbox"
                if prev.get("held") != "disc image":
                    more = ...
                    log(f"holding {name}: disc image ({imgs[0]}{more}) - Jellyfin cannot play "
                        f"disc images and {why}", "WARN")
                state[name] = {"shape": now["shape"], "stable": 0, "held": "disc image"}
                continue
            if convertible:
                # the .name file is part of the item's shape, so dropping one beside
                # a failed disc is a change that earns another attempt
                nf = name_file_for(path)
                if os.path.exists(nf):
                    st = os.stat(nf); now["shape"][os.path.basename(nf)] = [st.st_size, st.st_mtime]
                if prev.get("held") == "convert failed" and prev.get("shape") == now["shape"]:
                    state[name] = prev
                    continue
            ... (partial / declared / quiet / stable checks unchanged) ...
            if convertible:
                log(f"converting {name} ({human(now['bytes'])} disc image)")
                try:
                    convert_disc_image(cfg, name, log)
                    state.pop(name, None)
                except PickupError as e:
                    log(f"  convert FAILED, disc left in the inbox: {e}", "ERROR")
                    state[name] = {"shape": now["shape"], "stable": 0, "held": "convert failed", "last_error": str(e)}
                except Exception as e:
                    log(f"  convert FAILED unexpectedly, disc left in the inbox: {type(e).__name__}: {e}", "ERROR")
                    state[name] = {"shape": now["shape"], "stable": 0, "held": "convert failed", "last_error": f"{type(e).__name__}: {e}"}
                continue
            log(f"processing {name} ...")   # existing path
```

Note the existing `--force` flag: the stability checks are skipped, which is what the tests rely on.

- [ ] Step 4: run, all PASS (expect 66 old + new). Step 5: commit `pickup: remux held DVD ISOs into MKV items instead of holding them`.

### Task 5: Container and run script

**Files:** Modify `pickup/Dockerfile`, `pickup/run.sh`, `pickup/docker-entrypoint.sh`.

- [ ] Dockerfile: `apt-get install -y --no-install-recommends tzdata ffmpeg`, update the header comment ("stdlib plus Debian's ffmpeg for DVD images"), add `PARK_DIR=` to the ENV block.
- [ ] docker-entrypoint.sh: append `${PARK_DIR:+--park-dir $PARK_DIR}` to `args`.
- [ ] run.sh: `-v /mnt/incoming:/incoming` replacing the upload mount, `-e UPLOAD_DIR=/incoming/upload -e PARK_DIR=/incoming`.
- [ ] Commit `pickup: ship ffmpeg in the image and park converted discs beside the inbox`.

### Task 6: Deploy and prove

- [ ] `scp media_pickup.py tests/test_pickup.py Dockerfile run.sh docker-entrypoint.sh root@pve:/root/foxzilla-build/` (+ tests dir), run `python3 tests/test_pickup.py` there, `docker build -t foxzilla-pickup:latest .`, `./run.sh`.
- [ ] `echo 'The Snapper (1993)' > /mnt/incoming/upload/BBCDVD3090.iso.name`.
- [ ] Watch `/srv/foxzilla-config/pickup.log`: converting → converted → processing → committed for each disc; no `holding`.
- [ ] Verify: `ls /mnt/nas-media/movies/"My Left Foot (1989)"`, `/mnt/incoming/.removed-20261026/`, Jellyfin `GET /Items?SearchTerm=...` shows the three with `MediaSources`, and a `PlaybackInfo` call reports direct play.
- [ ] Mirror the build dir back to `~/foxzilla/pickup` if anything was edited on the host; commit.
