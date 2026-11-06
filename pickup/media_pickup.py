#!/usr/bin/env python3
"""
Media pickup watcher.  v2

Watches an SFTP drop folder for new movies/shows, waits for the upload to
go quiet, organizes it, hands it to the Jellyfin library roots and triggers a
rescan.

v2 exists because v1 lost episodes and duplicated files. The handoff is now a
three-phase transaction and the source is never touched until the destination
has been proven correct:

  Phase 1  MANIFEST   walk the incoming item; record every file's relative
                      path, size and SHA-256. This is the contract for what
                      must arrive, and it is what makes a missing episode a
                      detectable condition rather than an invisible one.
  Phase 2  STAGE      copy (never move) into a staging dir on the destination
                      filesystem, hashing as we write, and check every file
                      against the manifest. Reorganising happens here, where
                      it cannot touch the source.
  Phase 3  COMMIT     staging is on the destination filesystem, so each file
                      lands by atomic rename. Reconcile the placed files
                      against the manifest, and only then remove the source.

Invariants, all of which v1 broke:
  - Exactly one run at a time (flock). v1 raced itself every 5 minutes.
  - Nothing is ever deleted to make room for something else. Leftovers go to
    _leftovers/, conflicts go to quarantine. v1 called shutil.rmtree() on the
    source of a multi-movie pack.
  - A file already at the destination is compared by hash, not renamed to
    "name (1).mkv". Identical means skip, different means quarantine. The
    "(1)" suffix was the duplication the user kept seeing.
  - A failure anywhere leaves the source intact and the run retryable.

Run on a timer (cron, systemd, or the bundled container).
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request

# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

# Generic defaults. Real deployments pass --upload-dir/--movies-dest/etc, or
# set the matching environment variables via the container entrypoint.
DEFAULTS = {
    "upload_dir": "/data/incoming",
    "movies_dest": "/data/movies",
    "shows_dest": "/data/tv",
    "state_file": "/data/state.json",
    "log_file": "/data/pickup.log",
    "lock_file": os.path.join(
        "/run" if os.path.isdir("/run") else tempfile.gettempdir(),
        "media_pickup.lock"),
    "receipts_dir": "/data/receipts",
    "jellyfin_host": "jellyfin:8096",
    "park_dir": "",        # where .removed-YYYYMMDD/ goes; empty = the upload dir
    "status_file": "",     # live progress JSON for the dashboard; empty = off
}

# Credentials are resolved at runtime (env, then a 0600 config file) so that no
# password lives in this source file.
CRED_FILE = os.environ.get(
    "MEDIA_PICKUP_CREDENTIALS",
    os.path.expanduser("~/.config/media_pickup/credentials.json"))

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".m2ts"}
DISC_IMAGE_EXTS = {".iso", ".img"}   # DVD-Video images: remuxed to MKV, or held if ffmpeg is missing
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
# Files a transfer client leaves behind while it is still writing.
PARTIAL_EXTS = {".filepart", ".part", ".crdownload", ".!qb", ".tmp", ".partial"}
JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}

# An uploader that knows it has finished can say so by dropping a marker
# beside the item: ".<name>.complete". That replaces the stability guess with
# a fact, and is the difference between acting in seconds and waiting out two
# poll intervals plus a quiet period.
COMPLETE_SUFFIX = ".complete"


def marker_for(upload_dir: str, name: str) -> str:
    return os.path.join(upload_dir, f".{name}{COMPLETE_SUFFIX}")


def sweep_markers(upload_dir: str, entries) -> None:
    """Remove markers whose item is no longer here; they are just litter."""
    try:
        listing = os.listdir(upload_dir)
    except OSError:
        return
    for f in listing:
        if not (f.startswith(".") and f.endswith(COMPLETE_SUFFIX)):
            continue
        if f[1:-len(COMPLETE_SUFFIX)] not in entries:
            try:
                os.remove(os.path.join(upload_dir, f))
            except OSError:
                pass

EPISODE_PATTERNS = [
    re.compile(r"S\d{1,2}[\s._-]?E\d{1,3}", re.IGNORECASE),
    re.compile(r"\b\d{1,2}x\d{2}\b"),
    re.compile(r"\bEp(?:isode)?[\s._-]?\d{1,3}\b", re.IGNORECASE),
]
SEASON_DIR_PATTERN = re.compile(r"^(season|series|s)[\s._-]?\d{1,2}\b", re.IGNORECASE)
SEASON_NAME_PATTERN = re.compile(r"\b(season|series)[\s._-]?\d{1,2}\b", re.IGNORECASE)

# A candidate main feature must be within 40% of the largest video AND at
# least this big, so a 150 MB sample never gets promoted to a feature.
MIN_FEATURE_BYTES = 200 * 1024 * 1024
FEATURE_RATIO = 0.4

# A DVD rip whose largest video holds more than this share of all the video
# bytes is a film plus its extras, however many extras there are. Real movie
# discs measured 66-78%; a TV season's episodes are near-equal, and even a
# "play all" title only reaches about half, since it is the sum of the rest.
FEATURE_SHARE = 0.6

CHUNK = 4 * 1024 * 1024
STABLE_POLLS = 2          # consecutive unchanged observations before we act
QUIET_SECONDS = 60        # a file touched more recently than this is not quiet


class PickupError(Exception):
    """A run-fatal problem. The source is always left intact."""


# --------------------------------------------------------------------------
# logging and locking
# --------------------------------------------------------------------------

class Log:
    def __init__(self, path):
        self.path = path

    def __call__(self, msg, level="INFO"):
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {level:<5} {msg}"
        print(line)
        try:
            with open(self.path, "a") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


# --------------------------------------------------------------------------
# progress reporting (the dashboard's pickup card reads this file)
# --------------------------------------------------------------------------

class Progress:
    """
    Owns status.json: the active item's phase and progress, the inbox queue,
    and the last finished item. Writes are atomic (tmp + os.replace) and
    throttled. A write failure is logged once and otherwise ignored: the meter
    must never be the reason a handoff fails.
    """
    THROTTLE = 2.0      # seconds between progress writes
    WINDOW = 30.0       # rolling window for the rate
    MIN_SPAN = 5.0      # no rate until the samples span this long

    def __init__(self, path, clock=time.time, log=None):
        self.path, self.clock, self.log = path, clock, log
        self.writes = 0
        self._last_write = None
        self._samples = []
        self._failing = False
        self.doc = {"version": 1, "updated": None, "state": "idle",
                    "active": None, "queue": [], "last": self._read_last()}

    def _read_last(self):
        if not self.path:
            return None
        try:
            with open(self.path) as fh:
                return json.load(fh).get("last")
        except (OSError, ValueError, AttributeError):
            return None

    def queue(self, rows):
        active = (self.doc["active"] or {}).get("name")
        self.doc["queue"] = [r for r in rows if r.get("name") != active]

    def begin(self, name, phase, total, unit="bytes", files_total=None):
        now = self.clock()
        prev = self.doc["active"] or {}
        self.doc["state"] = "active"
        self.doc["active"] = {
            "name": name, "phase": phase, "unit": unit,
            "done": 0, "total": total, "file": None,
            "files_done": 0, "files_total": files_total,
            "rate": None, "eta_s": None, "phase_started": now,
            "item_started": prev.get("item_started", now) if prev.get("name") == name else now,
        }
        self.doc["queue"] = [r for r in self.doc["queue"] if r.get("name") != name]
        self._samples = [(now, 0)]
        self._write(force=True)

    def advance(self, n, file=None):
        a = self.doc["active"]
        if a:
            self.set_done(a["done"] + n, file)

    def set_done(self, done, file=None):
        a = self.doc["active"]
        if not a:
            return
        a["done"] = done
        if file is not None:
            a["file"] = file
        self._write()

    def file_done(self, file=None):
        a = self.doc["active"]
        if not a:
            return
        a["files_done"] += 1
        if a["unit"] == "files":
            a["done"] = a["files_done"]
        if file is not None:
            a["file"] = file
        self._write()

    def finish(self, name, result, error=None):
        self.doc["active"] = None
        self.doc["state"] = "idle"
        self.doc["last"] = {"name": name, "result": result,
                            "finished": self.clock(), "error": error}
        self._write(force=True)

    def idle(self):
        self.doc["active"] = None
        self.doc["state"] = "idle"
        self._write(force=True)

    def _rate(self, now):
        a = self.doc["active"]
        if not a or a["unit"] == "files":
            return
        self._samples.append((now, a["done"]))
        while len(self._samples) > 2 and self._samples[0][0] < now - self.WINDOW:
            self._samples.pop(0)
        (t0, d0), (t1, d1) = self._samples[0], self._samples[-1]
        if t1 - t0 < self.MIN_SPAN:
            a["rate"], a["eta_s"] = None, None
            return
        rate = (d1 - d0) / (t1 - t0)
        a["rate"] = round(rate, 3)
        a["eta_s"] = round((a["total"] - a["done"]) / rate, 1) if rate > 0 else None

    def _write(self, force=False):
        if not self.path:
            return
        now = self.clock()
        if not force and self._last_write is not None and now - self._last_write < self.THROTTLE:
            return
        self._last_write = now
        self._rate(now)
        self.doc["updated"] = now
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w") as fh:
                json.dump(self.doc, fh)
            os.chmod(tmp, 0o644)
            os.replace(tmp, self.path)
            self.writes += 1
            self._failing = False
        except OSError as e:
            if not self._failing and self.log:
                self.log(f"could not write status file {self.path}: {e}", "WARN")
            self._failing = True


NULL_PROGRESS = Progress("")


def acquire_lock(path):
    """
    Take an exclusive, non-blocking lock for the whole run.

    v1 had no lock and was scheduled every 5 minutes, so a long cross-device
    move of a box set was routinely overtaken by the next run; both then raced
    on the same item. That race is where the nested Show/Show/ directories and
    a good share of the duplicates came from.
    """
    fh = open(path, "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if e.errno in (errno.EACCES, errno.EAGAIN):
            return None
        raise
    fh.write(f"{os.getpid()}\n")
    fh.flush()
    return fh


# --------------------------------------------------------------------------
# hashing and manifests
# --------------------------------------------------------------------------

def sha256_file(path, hasher=None, on_chunk=None):
    h = hasher or hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            buf = fh.read(CHUNK)
            if not buf:
                break
            h.update(buf)
            if on_chunk:
                on_chunk(len(buf))
    return h.hexdigest()


def is_junk(name):
    low = name.lower()
    return low in JUNK_NAMES or low.startswith("._")


def walk_files(root):
    """Every real file under root, as (relative path, absolute path)."""
    out = []
    if os.path.isfile(root):
        return [(os.path.basename(root), root)]
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not is_junk(d)]
        for name in filenames:
            if is_junk(name):
                continue
            full = os.path.join(dirpath, name)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            out.append((os.path.relpath(full, root), full))
    return sorted(out)


def observe(root):
    """A cheap fingerprint of the item's current shape, for stability checks."""
    shape = {}
    partial = False
    for rel, full in walk_files(root):
        if os.path.splitext(rel)[1].lower() in PARTIAL_EXTS:
            partial = True
        try:
            st = os.stat(full)
        except OSError:
            continue
        shape[rel] = [st.st_size, st.st_mtime]
    newest = max((v[1] for v in shape.values()), default=0.0)
    return {
        "shape": shape,
        "files": len(shape),
        "bytes": sum(v[0] for v in shape.values()),
        "newest": newest,
        "partial": partial,
    }


def build_manifest(root, log, progress=NULL_PROGRESS):
    """Phase 1. The contract: what must exist at the destination when we finish."""
    found = walk_files(root)
    sizes = {rel: os.stat(full).st_size for rel, full in found}
    progress.begin(os.path.basename(root.rstrip("/")), "hashing",
                   sum(sizes.values()), files_total=len(found))
    files = []
    for rel, full in found:
        st = os.stat(full)
        files.append({
            "path": rel,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "sha256": sha256_file(full, on_chunk=lambda n, rel=rel: progress.advance(n, rel)),
        })
        progress.file_done(rel)
    total = sum(f["size"] for f in files)
    log(f"  phase 1: manifest built - {len(files)} files, {human(total)}")
    return {
        "root": os.path.basename(root.rstrip("/")),
        "built": time.time(),
        "files": files,
        "total_bytes": total,
    }


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}"
        n /= 1024.0


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------

def classify(name, rel_paths, sizes=None):
    """
    Decide movie vs TV.

    v1 looked only at names, so a DVD-ripped season whose files are called
    B1_t00.mkv fell through to the movie path - and the movie path then
    deleted every file below its "large file" threshold. Structure is checked
    here too, and the classifier is only ever allowed to be wrong in a way
    that is recoverable.

    `sizes` maps each rel path to its byte size. Without it, a movie disc
    ripped with five or more extras is indistinguishable from a season.
    """
    haystack = " ".join([name] + rel_paths)
    if any(p.search(haystack) for p in EPISODE_PATTERNS):
        return "tv", "episode marker in name"
    if SEASON_NAME_PATTERN.search(name):
        return "tv", "season marker in title"
    top_dirs = {p.split(os.sep)[0] for p in rel_paths if os.sep in p}
    if any(SEASON_DIR_PATTERN.match(d) for d in top_dirs):
        return "tv", "season subdirectory"
    vids = [p for p in rel_paths if os.path.splitext(p)[1].lower() in VIDEO_EXTS]
    if sizes and len(vids) >= 2:
        total = sum(sizes.get(v, 0) for v in vids)
        biggest = max(sizes.get(v, 0) for v in vids)
        if total and biggest / total > FEATURE_SHARE:
            return "movie", f"one feature holds {biggest * 100 // total}% of the video"
    if len(vids) >= 5:
        # Many similarly-sized videos with no movie structure is far more
        # likely a ripped season than a film with 5+ full-length extras.
        return "tv", f"{len(vids)} video files, no movie structure"
    return "movie", "default"


# --------------------------------------------------------------------------
# phase 2: staging
# --------------------------------------------------------------------------

def same_filesystem(a, b):
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def free_bytes(path):
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def stage(src_root, manifest, staging, log, dry_run=False, progress=NULL_PROGRESS):
    """
    Phase 2. Copy into staging on the destination filesystem and verify.

    Where the source and staging share a filesystem we hardlink instead of
    copying: instant, no extra space (which matters - / is nearly full), and
    the content is identical by construction. Otherwise we stream a copy and
    hash it as it is written, so verification costs no extra read.
    """
    os.makedirs(staging, exist_ok=True)
    linking = same_filesystem(src_root, staging)
    log(f"  phase 2: staging into {staging} ({'hardlink' if linking else 'copy'})")
    if not dry_run:
        progress.begin(os.path.basename(src_root.rstrip("/")),
                       "verifying" if linking else "copying",
                       manifest["total_bytes"], files_total=len(manifest["files"]))

    if not linking:
        need = manifest["total_bytes"]
        avail = free_bytes(os.path.dirname(staging))
        if avail < need * 1.05:
            raise PickupError(
                f"not enough room to stage: need {human(need)}, "
                f"{human(avail)} free on {os.path.dirname(staging)}")

    placed = []
    for entry in manifest["files"]:
        src = os.path.join(src_root, entry["path"]) if os.path.isdir(src_root) else src_root
        dst = os.path.join(staging, entry["path"])
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if dry_run:
            placed.append(entry["path"])
            continue

        if linking:
            try:
                os.link(src, dst)
            except OSError:
                linking = False
        if not linking or not os.path.exists(dst):
            h = hashlib.sha256()
            with open(src, "rb") as fin, open(dst, "wb") as fout:
                while True:
                    buf = fin.read(CHUNK)
                    if not buf:
                        break
                    fout.write(buf)
                    h.update(buf)
                    progress.advance(len(buf), entry["path"])
                fout.flush()
                os.fsync(fout.fileno())
            got = h.hexdigest()
        else:
            got = sha256_file(dst, on_chunk=lambda n, p=entry["path"]: progress.advance(n, p))

        if got != entry["sha256"]:
            raise PickupError(
                f"staging verification failed for {entry['path']}: "
                f"expected {entry['sha256'][:12]}, got {got[:12]}")
        if os.path.getsize(dst) != entry["size"]:
            raise PickupError(f"staging size mismatch for {entry['path']}")
        progress.file_done(entry["path"])
        placed.append(entry["path"])

    log(f"  phase 2: verified {len(placed)}/{len(manifest['files'])} files against manifest")
    return placed


# --------------------------------------------------------------------------
# organising (inside staging only)
# --------------------------------------------------------------------------

def video_files(root):
    return [rel for rel, _ in walk_files(root)
            if os.path.splitext(rel)[1].lower() in VIDEO_EXTS]


def organize_movie_staging(staging, title, log, renames=None):
    """
    Tidy a movie inside staging: promote the main feature, push the rest into
    extras/. Multi-movie packs are split into sibling folders.

    Unlike v1 this never deletes anything: whatever is not claimed by a movie
    goes to _leftovers/ for a human to look at.

    Every file moved is recorded in `renames` as
    {staged rel path: (target title, rel path inside that target)}, so
    reconciliation can follow it. Without that, a renamed feature looks
    missing and every movie with extras fails its own handoff.
    """
    if renames is None:
        renames = {}
    vids = video_files(staging)
    if len(vids) <= 1:
        return {title: staging}

    sizes = {v: os.path.getsize(os.path.join(staging, v)) for v in vids}
    ordered = sorted(vids, key=lambda v: sizes[v], reverse=True)
    biggest = sizes[ordered[0]]
    threshold = max(biggest * FEATURE_RATIO, MIN_FEATURE_BYTES)
    large = [v for v in ordered if sizes[v] >= threshold]

    if len(large) > 1:
        # One feature per file name. Upload clients sometimes send the same
        # film several times over (UUID-prefixed duplicate folders). Before
        # this, same-named copies were renamed onto each other, the survivor
        # was whichever came last - the smallest, i.e. a truncated transfer -
        # and reconciliation then failed on every retry, for hours.
        # `ordered` is largest first, so the largest copy of each name wins;
        # the rest are left in staging and fall through to _leftovers.
        features = {}
        for v in large:
            features.setdefault(os.path.splitext(os.path.basename(v))[0], v)
        dupes = len(large) - len(features)
        if dupes:
            log(f"  {dupes} same-named cop{'y' if dupes == 1 else 'ies'} of a feature "
                f"-> largest kept, the rest go to _leftovers")
        if len(features) > 1:
            log(f"  multi-movie pack: {len(features)} full-size features -> splitting")
        splits = {}
        holding = os.path.join(os.path.dirname(staging), f".split-{os.path.basename(staging)}")
        os.makedirs(holding, exist_ok=True)
        for movie_title, v in features.items():
            folder = os.path.join(holding, movie_title)
            os.makedirs(folder, exist_ok=True)
            os.rename(os.path.join(staging, v), os.path.join(folder, os.path.basename(v)))
            renames[v] = (movie_title, os.path.basename(v))
            splits[movie_title] = folder
        # Everything not claimed by a feature is kept, not deleted.
        remaining = walk_files(staging)
        if remaining:
            leftovers = os.path.join(holding, "_leftovers", title)
            for rel, full in remaining:
                target = os.path.join(leftovers, rel)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                os.rename(full, target)
                renames[rel] = (f"_leftovers/{title}", rel)
            log(f"  kept {len(remaining)} unclaimed file(s) in _leftovers/{title}")
            splits[f"_leftovers/{title}"] = leftovers
        shutil.rmtree(staging, ignore_errors=True)   # now genuinely empty
        return splits

    feature = ordered[0]
    ext = os.path.splitext(feature)[1]
    wanted = f"{title}{ext}"
    if feature != wanted:
        os.replace(os.path.join(staging, feature), os.path.join(staging, wanted))
        renames[feature] = (title, wanted)
    extras = os.path.join(staging, "extras")
    os.makedirs(extras, exist_ok=True)
    moved = 0
    for v in ordered[1:]:
        src = os.path.join(staging, v)
        if not os.path.exists(src) or os.path.dirname(v) == "extras":
            continue
        target_name = os.path.basename(v)
        if os.path.exists(os.path.join(extras, target_name)):
            # Same name from different folders (Featurettes/sample.mkv,
            # Trailers/sample.mkv): flattening into extras/ would clobber
            # one and fail reconciliation. Keep both, tagged by folder.
            stem, ext = os.path.splitext(target_name)
            parent = os.path.basename(os.path.dirname(v)) or "copy"
            target_name, n = f"{stem} ({parent}){ext}", 2
            while os.path.exists(os.path.join(extras, target_name)):
                target_name, n = f"{stem} ({parent} {n}){ext}", n + 1
        os.replace(src, os.path.join(extras, target_name))
        renames[v] = (title, os.path.join("extras", target_name))
        moved += 1
    log(f"  organized movie: feature={wanted}, {moved} extra(s)")
    return {title: staging}


# --------------------------------------------------------------------------
# phase 3: commit
# --------------------------------------------------------------------------

def commit(staging, dest, quarantine, log, dry_run=False, progress=NULL_PROGRESS):
    """
    Phase 3. Move staging into place one atomic rename at a time.

    Staging shares a filesystem with the destination, so each rename is atomic
    and cannot half-write a file. A name that already exists is resolved by
    comparing hashes - identical is a no-op, different goes to quarantine.
    That replaces v1's "name (1).mkv" suffixing, which is what turned every
    retry into a duplicate.
    """
    placements, skipped, conflicts = {}, [], []
    if dry_run:
        return placements, skipped, conflicts

    if not os.path.exists(dest):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        os.rename(staging, dest)
        for rel, _ in walk_files(dest):
            placements[rel] = os.path.join(dest, rel)
            progress.file_done(rel)
        log(f"  phase 3: committed as a whole -> {dest}")
        return placements, skipped, conflicts

    log(f"  phase 3: destination exists, merging by content -> {dest}")
    for rel, full in walk_files(staging):
        progress.file_done(rel)
        target = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if os.path.exists(target):
            if os.path.getsize(target) == os.path.getsize(full) \
                    and sha256_file(target) == sha256_file(full):
                skipped.append(rel)
                os.remove(full)
                placements[rel] = target
                continue
            qtarget = os.path.join(quarantine, os.path.basename(dest), rel)
            os.makedirs(os.path.dirname(qtarget), exist_ok=True)
            os.rename(full, qtarget)
            conflicts.append((rel, qtarget))
            continue
        os.rename(full, target)
        placements[rel] = target

    prune_empty(staging)
    if skipped:
        log(f"  {len(skipped)} file(s) already present with identical content - skipped")
    if conflicts:
        log(f"  {len(conflicts)} name clash(es) with different content -> quarantine", "WARN")
    return placements, skipped, conflicts


def prune_empty(root):
    """
    Remove now-empty directories, deepest first.

    v1's merge_into() removed a directory in the recursive call and then
    removed it again in the caller, raising FileNotFoundError partway through
    the loop over seasons - which is precisely how a box set ended up
    half-moved. Walking bottom-up once, and tolerating an already-gone
    directory, is all this ever needed to be.
    """
    if not os.path.isdir(root):
        return
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        if filenames or any(is_junk(f) for f in filenames):
            continue
        try:
            os.rmdir(dirpath)
        except OSError:
            pass


def reconcile(manifest, placements, skipped, conflicts, log):
    """
    Every file in the manifest must be accounted for at the destination.

    Content was proven by hash in phase 2 and each file arrived by atomic
    same-filesystem rename, so size is a sufficient check here; the point of
    this phase is completeness, which is the failure v1 could not detect.
    """
    missing, wrong = [], []
    quarantined = {rel for rel, _ in conflicts}
    for entry in manifest["files"]:
        rel = entry["path"]
        if rel in quarantined:
            continue
        target = placements.get(rel)
        if not target or not os.path.exists(target):
            missing.append(rel)
            continue
        if os.path.getsize(target) != entry["size"]:
            wrong.append(rel)
    accounted = len(manifest["files"]) - len(missing) - len(wrong)
    log(f"  phase 3: reconciled {accounted}/{len(manifest['files'])} files"
        + (f", {len(quarantined)} quarantined" if quarantined else ""))
    if missing or wrong:
        raise PickupError(
            f"reconciliation failed: {len(missing)} missing, {len(wrong)} wrong size"
            + (f" (first missing: {missing[0]})" if missing else ""))
    return accounted


# --------------------------------------------------------------------------
# Jellyfin
# --------------------------------------------------------------------------

def read_cred_file():
    try:
        with open(CRED_FILE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def write_cred_file(data):
    os.makedirs(os.path.dirname(CRED_FILE), mode=0o700, exist_ok=True)
    tmp = CRED_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CRED_FILE)
    os.chmod(os.path.dirname(CRED_FILE), 0o700)


def load_credentials(host=None, log=None):
    """
    Resolve a Jellyfin credential. No password is ever stored or embedded.

    Order: environment, then the 0600 credential file written by --login.
    What --login persists is the *access token* Jellyfin hands back, so each
    operator authenticates on their own behalf and their password never
    reaches disk. A legacy file containing a password is migrated here: the
    password is exchanged for a token once and then erased.
    """
    for key, field in (("JELLYFIN_API_KEY", "api_key"), ("JELLYFIN_TOKEN", "token")):
        if os.environ.get(key):
            return {field: os.environ[key], "user": os.environ.get("JELLYFIN_USER")}
    env_user, env_pass = os.environ.get("JELLYFIN_USER"), os.environ.get("JELLYFIN_PASSWORD")
    if env_user and env_pass and host:
        # Supplied for this process only; still exchanged rather than kept.
        return {"token": jf_authenticate(host, env_user, env_pass), "user": env_user}

    creds = read_cred_file()
    if creds.get("password") and host:
        try:
            token = jf_authenticate(host, creds["user"], creds["password"])
        except Exception as e:
            log and log(f"could not migrate stored password to a token: {e}", "WARN")
            return creds
        creds = {"user": creds.get("user"), "token": token,
                 "created": time.time(), "migrated_from_password": True}
        write_cred_file(creds)
        log and log("exchanged the stored password for an access token and erased it")
    return creds


def cmd_login(host, user=None, api_key=None):
    """
    Authenticate on your own behalf and store only the resulting token.

    Run it as yourself: `media_pickup.py --login`. Your password is sent to
    Jellyfin once and never written to disk.
    """
    import getpass
    if api_key:
        write_cred_file({"api_key": api_key, "created": time.time()})
        print(f"Stored Jellyfin API key in {CRED_FILE} (0600).")
        return 0
    user = user or input("Jellyfin username: ").strip()
    if not user:
        print("No username given.", file=sys.stderr)
        return 1
    password = getpass.getpass(f"Jellyfin password for {user}: ")
    try:
        token = jf_authenticate(host, user, password)
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError) as e:
        print(f"Login failed: {e}", file=sys.stderr)
        return 1
    finally:
        del password
    write_cred_file({"user": user, "token": token, "created": time.time()})
    print(f"Logged in as {user}. Access token stored in {CRED_FILE} (0600); "
          "your password was not saved.")
    return 0


def cmd_logout():
    try:
        os.remove(CRED_FILE)
        print(f"Removed {CRED_FILE}.")
    except FileNotFoundError:
        print("No stored credential.")
    return 0


def cmd_whoami():
    creds = read_cred_file()
    if not creds:
        print("Not logged in. Run:  media_pickup.py --login")
        return 1
    if creds.get("api_key"):
        print("Authenticated with a Jellyfin API key.")
    else:
        when = time.strftime("%Y-%m-%d %H:%M",
                             time.localtime(creds.get("created", 0)))
        print(f"Logged in as {creds.get('user')} (token stored {when}).")
    if creds.get("password"):
        print("WARNING: this file still contains a password; it will be "
              "exchanged for a token on the next run.")
    return 0


def jf_headers(token=None):
    auth = ('MediaBrowser Client="MediaPickup", Device="MediaPickup", '
            'DeviceId="media-pickup-1", Version="2.0.0"')
    if token:
        auth += f', Token="{token}"'
    return {"X-Emby-Authorization": auth}


def jf_authenticate(host, user, password):
    req = urllib.request.Request(
        f"http://{host}/Users/AuthenticateByName",
        data=json.dumps({"Username": user, "Pw": password}).encode(),
        headers={**jf_headers(), "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)["AccessToken"]


def jf_rescan(host, creds, log):
    """Trigger a library refresh with whatever credential is configured."""
    try:
        if creds.get("api_key"):
            headers = {"X-Emby-Token": creds["api_key"]}
        else:
            token = creds.get("token")
            if not token:
                log("no Jellyfin credential - run 'media_pickup.py --login'; "
                    "rescan skipped", "WARN")
                return
            headers = jf_headers(token)
        req = urllib.request.Request(f"http://{host}/Library/Refresh",
                                     headers=headers, method="POST", data=b"")
        with urllib.request.urlopen(req, timeout=15) as r:
            log(f"triggered Jellyfin rescan (HTTP {r.status})")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            log("Jellyfin rejected the stored token - run "
                "'media_pickup.py --login' to re-authenticate", "WARN")
        else:
            log(f"could not trigger Jellyfin rescan: {e}", "WARN")
    except (urllib.error.URLError, KeyError) as e:
        log(f"could not trigger Jellyfin rescan: {e}", "WARN")


def fold(s):
    """Accent- and case-insensitive form for comparing titles."""
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c)).casefold().strip()


def pick_movie_match(results, title):
    """
    First TheMovieDb result whose name starts with ours -> (name, year).

    An exact match keeps the provider's spelling (accents); a longer provider
    title ("My Left Foot: The Story of ...") keeps our title and takes the year.
    """
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
    """'Title (Year)' as a single safe path component."""
    safe = re.sub(r"[/\\]", "-", title).replace(":", "").strip(". ")
    safe = re.sub(r"\s+", " ", safe)
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
        log("  no Jellyfin credential - run 'media_pickup.py --login' to get years for discs", "WARN")
        return None
    req = urllib.request.Request(
        f"http://{host}/Items/RemoteSearch/Movie",
        data=json.dumps({"SearchInfo": {"Name": title}}).encode(),
        headers={**headers, "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return pick_movie_match(json.load(r), title)


# --------------------------------------------------------------------------
# disc images: ffprobe/ffmpeg (Debian's build has the dvdvideo demuxer, which
# reads a DVD-Video ISO directly - no loop mount, no privileges)
# --------------------------------------------------------------------------

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
        p = subprocess.run([FFPROBE, "-v", "error", "-f", "dvdvideo", "-title", str(n),
                            "-i", iso, "-show_entries", "format=duration", "-of", "csv=p=0"],
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
    """Lossless: the title's video, every audio and subtitle stream, chapters."""
    cmd = [FFMPEG, "-nostdin", "-y", "-v", "error", "-f", "dvdvideo", "-title", str(title_no),
           "-i", iso, "-map", "0", "-c", "copy", "-f", "matroska", out_path]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        tail = " | ".join(l for l in p.stderr.strip().splitlines()[-5:] if l.strip())
        raise PickupError(f"ffmpeg failed (rc {p.returncode}): {tail or 'no output'}")
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise PickupError("ffmpeg produced no output")


NAME_SUFFIX = ".name"         # "<disc>.iso.name": one line, the item name to use
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
        creds = load_credentials(cfg["jellyfin_host"], log)
        match = jf_movie_lookup(cfg["jellyfin_host"], creds, title, log)
    except Exception as e:
        match = None
        log(f"  could not look up the year for '{title}': {e}", "WARN")
    if not match:
        log(f"  no year found for '{title}' - filing it by label; identify it in Jellyfin", "WARN")
        return movie_item_name(title, None), "label only"
    return movie_item_name(*match), f"label + Jellyfin: {match[0]} ({match[1]})"


def convert_disc_image(cfg, name, log):
    """
    Remux a DVD image in the inbox into an MKV item the handoff will file.

    The MKV is written under .converting/ and renamed into the inbox whole, so
    a tick never sees a half-written item; then the ISO (and its .name file)
    is renamed into the park dir. Nothing is deleted. Returns the item name.
    """
    iso = os.path.join(cfg["upload_dir"], name)
    started = time.time()
    park_root = park_dir_today(cfg)
    park_base = os.path.dirname(park_root)
    if not os.path.isdir(park_base) or not same_filesystem(cfg["upload_dir"], park_base):
        raise PickupError(f"park dir {park_base} is missing or not on the inbox filesystem")
    item, why = disc_item_name(cfg, iso, log)
    final = os.path.join(cfg["upload_dir"], item)
    if os.path.exists(final):
        raise PickupError(f"converted item already in the inbox: {item}")
    found = probe_titles(iso)
    best = longest_title(found)
    if not best:
        raise PickupError("no readable DVD titles on the disc")
    log(f"  title {best[0]} ({best[1] / 60:.1f} min, longest of {len(found)} readable) "
        f"-> {item}/{item}.mkv ({why})")
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
        fh.write(f"converted from {name}\n")
    parked = park(iso, park_root)
    if os.path.exists(name_file_for(iso)):
        park(name_file_for(iso), park_root)
    prune_empty(os.path.join(cfg["upload_dir"], CONVERTING_DIR))
    log(f"  -> converted in {time.time() - started:.0f}s; {name} parked at {parked}")
    return item


# --------------------------------------------------------------------------
# processing one item
# --------------------------------------------------------------------------

def guess_show_name(filename):
    base = os.path.splitext(filename)[0]
    for pattern in EPISODE_PATTERNS[:2]:
        m = pattern.search(base)
        if m:
            base = base[:m.start()]
            break
    base = base.replace(".", " ").replace("_", " ").strip(" -")
    return base or os.path.splitext(filename)[0]


def write_receipt(cfg, name, payload, log):
    try:
        os.makedirs(cfg["receipts_dir"], mode=0o700, exist_ok=True)
        safe = re.sub(r"[^\w.-]+", "_", name)[:120]
        path = os.path.join(cfg["receipts_dir"],
                            f"{time.strftime('%Y%m%d-%H%M%S')}-{safe}.json")
        with open(path, "w") as fh:
            json.dump(payload, fh, indent=2)
        return path
    except OSError as e:
        log(f"could not write receipt: {e}", "WARN")
        return None


def process_item(cfg, name, log, dry_run=False, progress=NULL_PROGRESS):
    """Run the three phases for one incoming item. Raises PickupError on failure."""
    src = os.path.join(cfg["upload_dir"], name)
    started = time.time()

    manifest = build_manifest(src, log, progress)
    if not manifest["files"]:
        raise PickupError("item contains no usable files")

    rel_paths = [f["path"] for f in manifest["files"]]
    sizes = {f["path"]: f["size"] for f in manifest["files"]}
    kind, why = classify(name, rel_paths, sizes)
    log(f"  classified as {kind.upper()} ({why})")

    if kind == "tv":
        dest_root = cfg["shows_dest"]
        title = guess_show_name(name) if os.path.isfile(src) else name
    else:
        dest_root = cfg["movies_dest"]
        title = os.path.splitext(name)[0] if os.path.isfile(src) else name

    staging_base = os.path.join(dest_root, ".staging")
    quarantine = os.path.join(dest_root, ".quarantine")
    os.makedirs(staging_base, exist_ok=True)
    staging = os.path.join(staging_base, f"{os.getpid()}-{int(started)}")

    placements, skipped, conflicts = {}, [], []
    renames = {}
    try:
        stage(src, manifest, staging, log, dry_run=dry_run, progress=progress)

        if kind == "movie" and not dry_run:
            targets = organize_movie_staging(staging, title, log, renames)
        else:
            targets = {title: staging}

        # commit() reports paths inside each target; map them back to the
        # manifest's paths, which is what reconcile() checks against.
        back = {where: rel for rel, where in renames.items()}
        if not dry_run:
            progress.begin(name, "committing", len(manifest["files"]), unit="files",
                           files_total=len(manifest["files"]))
        for target_title, staged_path in targets.items():
            dest = os.path.join(dest_root, target_title)
            p, s, c = commit(staged_path, dest, quarantine, log, dry_run=dry_run,
                           progress=progress)
            orig = lambda rel: back.get((target_title, rel), rel)
            placements.update((orig(rel), target) for rel, target in p.items())
            skipped.extend(orig(rel) for rel in s)
            conflicts.extend((orig(rel), q) for rel, q in c)

        if not dry_run:
            reconcile(manifest, placements, skipped, conflicts, log)
    except Exception:
        # The source has not been touched; leave staging for inspection.
        if os.path.isdir(staging) and not os.listdir(staging):
            os.rmdir(staging)
        raise

    dests = sorted({os.path.join(dest_root, t) for t in targets})
    if not dry_run:
        # Only now is it safe: every file is verified and accounted for.
        if os.path.isdir(src):
            shutil.rmtree(src)
        else:
            os.remove(src)
        prune_empty(staging_base)

    elapsed = time.time() - started
    log(f"  -> {kind.upper()} committed to {'; '.join(dests)} in {elapsed:.1f}s")
    write_receipt(cfg, name, {
        "item": name, "kind": kind, "why": why,
        "destinations": dests,
        "files": len(manifest["files"]),
        "bytes": manifest["total_bytes"],
        "skipped_identical": skipped,
        "quarantined": [rel for rel, _ in conflicts],
        "seconds": round(elapsed, 1),
        "manifest": manifest["files"],
    }, log)
    return dests


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def load_state(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, path)


def build_config(args):
    cfg = dict(DEFAULTS)
    for key in cfg:
        val = getattr(args, key, None)
        if val:
            cfg[key] = val
    return cfg


def main(argv=None):
    ap = argparse.ArgumentParser(description="Media pickup watcher (three-phase, verified).")
    for key, val in DEFAULTS.items():
        ap.add_argument(f"--{key.replace('_', '-')}", dest=key, default=None,
                        help=f"default: {val}")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would happen; touch nothing")
    ap.add_argument("--force", action="store_true",
                    help="skip the stability wait and process now")
    ap.add_argument("--login", action="store_true",
                    help="authenticate to Jellyfin as yourself; stores a token, not your password")
    ap.add_argument("--login-user", help="username for --login (otherwise prompted)")
    ap.add_argument("--api-key", help="store a Jellyfin API key instead of logging in")
    ap.add_argument("--logout", action="store_true", help="remove the stored credential")
    ap.add_argument("--whoami", action="store_true", help="show who is authenticated")
    args = ap.parse_args(argv)
    cfg = build_config(args)

    # --api-key is a credential action in its own right; requiring --login
    # alongside it meant it silently fell through to a normal watcher run.
    if args.login or args.api_key:
        return cmd_login(cfg["jellyfin_host"], args.login_user, args.api_key)
    if args.logout:
        return cmd_logout()
    if args.whoami:
        return cmd_whoami()

    log = Log(cfg["log_file"])

    if not os.path.isdir(cfg["upload_dir"]):
        log(f"upload dir {cfg['upload_dir']} missing", "ERROR")
        return 1

    lock = acquire_lock(cfg["lock_file"])
    if lock is None:
        log("another run is still working - skipping this tick")
        return 0

    try:
        entries = [e for e in sorted(os.listdir(cfg["upload_dir"]))
                   if not e.startswith(".") and not is_junk(e)
                   and not e.endswith(NAME_SUFFIX)]
        if not entries:
            # Still tidy up: a marker whose item never arrived, or was already
            # dealt with, would otherwise sit there indefinitely.
            sweep_markers(cfg["upload_dir"], entries)
            return 0

        state = load_state(cfg["state_file"])
        processed = []

        for name in entries:
            path = os.path.join(cfg["upload_dir"], name)
            now = observe(path)
            prev = state.get(name, {})

            marker = marker_for(cfg["upload_dir"], name)
            declared = os.path.exists(marker)

            imgs = [p for p in now["shape"] if os.path.splitext(p)[1].lower() in DISC_IMAGE_EXTS]
            convertible = bool(imgs) and os.path.isfile(path) and ffmpeg_available()
            if imgs and not convertible:
                # A disc image we cannot remux here. Filing it would only hide
                # it: Jellyfin ignores .iso. Leave it where the uploader can
                # see it and say so once, not every tick.
                why = ("ffmpeg is not installed" if os.path.isfile(path)
                       else "put the .iso at the top level of the inbox to have it remuxed")
                if prev.get("held") != "disc image":
                    more = f" +{len(imgs) - 1} more" if len(imgs) > 1 else ""
                    log(f"holding {name}: disc image ({imgs[0]}{more}) - Jellyfin cannot play "
                        f"disc images; convert to MKV and it will be picked up ({why})", "WARN")
                state[name] = {"shape": now["shape"], "stable": 0, "held": "disc image"}
                continue
            if convertible:
                # The .name file is part of the disc's shape, so dropping one
                # beside a disc that failed to convert is the change that earns
                # another attempt. An unchanged failed disc is left alone.
                nf = name_file_for(path)
                if os.path.exists(nf):
                    st = os.stat(nf)
                    now["shape"][os.path.basename(nf)] = [st.st_size, st.st_mtime]
                if prev.get("held") == "convert failed" and prev.get("shape") == now["shape"]:
                    state[name] = prev
                    continue
            if now["partial"] and not declared:
                log(f"watching {name}: transfer still in progress (partial files)")
                state[name] = {"shape": now["shape"], "stable": 0}
                continue
            if declared:
                # The uploader has verified this item and said so. Trusting
                # that is safe: the manifest is rebuilt and every file is
                # hashed again before anything is deleted, so a marker written
                # too early costs a failed run, never data.
                log(f"{name}: upload marked complete by the sender")
            elif time.time() - now["newest"] < QUIET_SECONDS and not args.force:
                log(f"watching {name}: written to in the last {QUIET_SECONDS}s")
                state[name] = {"shape": now["shape"], "stable": 0}
                continue

            stable = prev.get("stable", 0) + 1 if prev.get("shape") == now["shape"] else 0
            if stable < STABLE_POLLS and not args.force and not declared:
                log(f"watching {name}: {now['files']} files, {human(now['bytes'])} "
                    f"(stable {stable}/{STABLE_POLLS})")
                state[name] = {"shape": now["shape"], "stable": stable}
                continue

            if convertible:
                if args.dry_run:
                    log(f"would convert {name} ({human(now['bytes'])} disc image)")
                    continue
                log(f"converting {name} ({human(now['bytes'])} disc image)")
                try:
                    item = convert_disc_image(cfg, name, log)
                    state.pop(name, None)
                    # The new item joins this tick's listing: the loop reaches
                    # it next (its marker lets it skip the stability wait, so
                    # it is filed straight away) and its marker is not swept.
                    entries.append(item)
                except PickupError as e:
                    log(f"  convert FAILED, disc left in the inbox: {e}", "ERROR")
                    state[name] = {"shape": now["shape"], "stable": 0,
                                   "held": "convert failed", "last_error": str(e)}
                except Exception as e:
                    log(f"  convert FAILED unexpectedly, disc left in the inbox: "
                        f"{type(e).__name__}: {e}", "ERROR")
                    state[name] = {"shape": now["shape"], "stable": 0,
                                   "held": "convert failed", "last_error": f"{type(e).__name__}: {e}"}
                continue

            log(f"processing {name} ({now['files']} files, {human(now['bytes'])})")
            try:
                dests = process_item(cfg, name, log, dry_run=args.dry_run)
                processed.extend(dests)
                state.pop(name, None)
                if not args.dry_run:
                    try:
                        os.remove(marker)
                    except OSError:
                        pass
            except PickupError as e:
                log(f"  FAILED, source left intact: {e}", "ERROR")
                state[name] = {"shape": now["shape"], "stable": 0,
                               "last_error": str(e)}
            except Exception as e:
                log(f"  UNEXPECTED FAILURE, source left intact: "
                    f"{type(e).__name__}: {e}", "ERROR")
                state[name] = {"shape": now["shape"], "stable": 0,
                               "last_error": f"{type(e).__name__}: {e}"}

        for name in list(state):
            if name not in entries:
                state.pop(name)
        sweep_markers(cfg["upload_dir"], entries)
        save_state(cfg["state_file"], state)

        if processed and not args.dry_run:
            jf_rescan(cfg["jellyfin_host"],
                      load_credentials(cfg["jellyfin_host"], log), log)
        return 0
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
