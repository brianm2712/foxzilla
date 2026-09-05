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
import sys
import tempfile
import time
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
}

# Credentials are resolved at runtime (env, then a 0600 config file) so that no
# password lives in this source file.
CRED_FILE = os.environ.get(
    "MEDIA_PICKUP_CREDENTIALS",
    os.path.expanduser("~/.config/media_pickup/credentials.json"))

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".m2ts"}
# Files a transfer client leaves behind while it is still writing.
PARTIAL_EXTS = {".filepart", ".part", ".crdownload", ".!qb", ".tmp", ".partial"}
JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}

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

def sha256_file(path, hasher=None):
    h = hasher or hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            buf = fh.read(CHUNK)
            if not buf:
                break
            h.update(buf)
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


def build_manifest(root, log):
    """Phase 1. The contract: what must exist at the destination when we finish."""
    files = []
    for rel, full in walk_files(root):
        st = os.stat(full)
        files.append({
            "path": rel,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "sha256": sha256_file(full),
        })
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

def classify(name, rel_paths):
    """
    Decide movie vs TV.

    v1 looked only at names, so a DVD-ripped season whose files are called
    B1_t00.mkv fell through to the movie path - and the movie path then
    deleted every file below its "large file" threshold. Structure is checked
    here too, and the classifier is only ever allowed to be wrong in a way
    that is recoverable.
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


def stage(src_root, manifest, staging, log, dry_run=False):
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
                fout.flush()
                os.fsync(fout.fileno())
            got = h.hexdigest()
        else:
            got = sha256_file(dst)

        if got != entry["sha256"]:
            raise PickupError(
                f"staging verification failed for {entry['path']}: "
                f"expected {entry['sha256'][:12]}, got {got[:12]}")
        if os.path.getsize(dst) != entry["size"]:
            raise PickupError(f"staging size mismatch for {entry['path']}")
        placed.append(entry["path"])

    log(f"  phase 2: verified {len(placed)}/{len(manifest['files'])} files against manifest")
    return placed


# --------------------------------------------------------------------------
# organising (inside staging only)
# --------------------------------------------------------------------------

def video_files(root):
    return [rel for rel, _ in walk_files(root)
            if os.path.splitext(rel)[1].lower() in VIDEO_EXTS]


def organize_movie_staging(staging, title, log):
    """
    Tidy a movie inside staging: promote the main feature, push the rest into
    extras/. Multi-movie packs are split into sibling folders.

    Unlike v1 this never deletes anything: whatever is not claimed by a movie
    goes to _leftovers/ for a human to look at.
    """
    vids = video_files(staging)
    if len(vids) <= 1:
        return {title: staging}

    sizes = {v: os.path.getsize(os.path.join(staging, v)) for v in vids}
    ordered = sorted(vids, key=lambda v: sizes[v], reverse=True)
    biggest = sizes[ordered[0]]
    threshold = max(biggest * FEATURE_RATIO, MIN_FEATURE_BYTES)
    large = [v for v in ordered if sizes[v] >= threshold]

    if len(large) > 1:
        log(f"  multi-movie pack: {len(large)} full-size features -> splitting")
        splits = {}
        holding = os.path.join(os.path.dirname(staging), f".split-{os.path.basename(staging)}")
        os.makedirs(holding, exist_ok=True)
        for v in large:
            movie_title = os.path.splitext(os.path.basename(v))[0]
            folder = os.path.join(holding, movie_title)
            os.makedirs(folder, exist_ok=True)
            os.rename(os.path.join(staging, v), os.path.join(folder, os.path.basename(v)))
            splits[movie_title] = folder
        # Everything not claimed by a feature is kept, not deleted.
        remaining = walk_files(staging)
        if remaining:
            leftovers = os.path.join(holding, "_leftovers", title)
            for rel, full in remaining:
                target = os.path.join(leftovers, rel)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                os.rename(full, target)
            log(f"  kept {len(remaining)} unclaimed file(s) in _leftovers/{title}")
            splits[f"_leftovers/{title}"] = leftovers
        shutil.rmtree(staging, ignore_errors=True)   # now genuinely empty
        return splits

    feature = ordered[0]
    ext = os.path.splitext(feature)[1]
    wanted = f"{title}{ext}"
    if feature != wanted:
        os.replace(os.path.join(staging, feature), os.path.join(staging, wanted))
    extras = os.path.join(staging, "extras")
    os.makedirs(extras, exist_ok=True)
    moved = 0
    for v in ordered[1:]:
        src = os.path.join(staging, v)
        if not os.path.exists(src) or os.path.dirname(v) == "extras":
            continue
        os.replace(src, os.path.join(extras, os.path.basename(v)))
        moved += 1
    log(f"  organized movie: feature={wanted}, {moved} extra(s)")
    return {title: staging}


# --------------------------------------------------------------------------
# phase 3: commit
# --------------------------------------------------------------------------

def commit(staging, dest, quarantine, log, dry_run=False):
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
        log(f"  phase 3: committed as a whole -> {dest}")
        return placements, skipped, conflicts

    log(f"  phase 3: destination exists, merging by content -> {dest}")
    for rel, full in walk_files(staging):
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


def process_item(cfg, name, log, dry_run=False):
    """Run the three phases for one incoming item. Raises PickupError on failure."""
    src = os.path.join(cfg["upload_dir"], name)
    started = time.time()

    manifest = build_manifest(src, log)
    if not manifest["files"]:
        raise PickupError("item contains no usable files")

    rel_paths = [f["path"] for f in manifest["files"]]
    kind, why = classify(name, rel_paths)
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
    try:
        stage(src, manifest, staging, log, dry_run=dry_run)

        if kind == "movie" and not dry_run:
            targets = organize_movie_staging(staging, title, log)
        else:
            targets = {title: staging}

        for target_title, staged_path in targets.items():
            dest = os.path.join(dest_root, target_title)
            p, s, c = commit(staged_path, dest, quarantine, log, dry_run=dry_run)
            placements.update(p)
            skipped.extend(s)
            conflicts.extend(c)

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

    if args.login:
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
                   if not e.startswith(".") and not is_junk(e)]
        if not entries:
            return 0

        state = load_state(cfg["state_file"])
        processed = []

        for name in entries:
            path = os.path.join(cfg["upload_dir"], name)
            now = observe(path)
            prev = state.get(name, {})

            if now["partial"]:
                log(f"watching {name}: transfer still in progress (partial files)")
                state[name] = {"shape": now["shape"], "stable": 0}
                continue
            if time.time() - now["newest"] < QUIET_SECONDS and not args.force:
                log(f"watching {name}: written to in the last {QUIET_SECONDS}s")
                state[name] = {"shape": now["shape"], "stable": 0}
                continue

            stable = prev.get("stable", 0) + 1 if prev.get("shape") == now["shape"] else 0
            if stable < STABLE_POLLS and not args.force:
                log(f"watching {name}: {now['files']} files, {human(now['bytes'])} "
                    f"(stable {stable}/{STABLE_POLLS})")
                state[name] = {"shape": now["shape"], "stable": stable}
                continue

            log(f"processing {name} ({now['files']} files, {human(now['bytes'])})")
            try:
                dests = process_item(cfg, name, log, dry_run=args.dry_run)
                processed.extend(dests)
                state.pop(name, None)
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
        save_state(cfg["state_file"], state)

        if processed and not args.dry_run:
            jf_rescan(cfg["jellyfin_host"],
                      load_credentials(cfg["jellyfin_host"], log), log)
        return 0
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
