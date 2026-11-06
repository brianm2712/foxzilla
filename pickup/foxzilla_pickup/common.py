"""Shared constants, config defaults, logging, locking, hashing and file walking."""
from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import tempfile
import time


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


VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".m2ts"}
DISC_IMAGE_EXTS = {".iso", ".img"}   # DVD-Video images: remuxed to MKV, or held if ffmpeg is missing


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


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}"
        n /= 1024.0


def same_filesystem(a, b):
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def free_bytes(path):
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


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
