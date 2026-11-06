"""DVD disc images: ISO label/title, ffprobe/ffmpeg wrappers, conversion, parking."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time

# same_filesystem is called as common.same_filesystem and jf_movie_lookup as
# jellyfin.jf_movie_lookup, so tests patch each in one place.
from . import common, jellyfin
from .common import PickupError, marker_for, prune_empty
from .classify import movie_item_name
from .jellyfin import load_credentials
from .progress import NULL_PROGRESS


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


def remux_title(iso, title_no, out_path, on_time=None):
    """Lossless: the title's video, every audio and subtitle stream, chapters."""
    cmd = [FFMPEG, "-nostdin", "-y", "-v", "error", "-progress", "pipe:1", "-nostats",
           "-f", "dvdvideo", "-title", str(title_no),
           "-i", iso, "-map", "0", "-c", "copy", "-f", "matroska", out_path]
    # stderr goes to a file, not a pipe: reading stdout to the end while
    # stderr fills its pipe buffer would deadlock ffmpeg.
    with tempfile.TemporaryFile(mode="w+", errors="replace") as err:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, text=True,
                             errors="replace")
        try:
            for line in p.stdout:
                if on_time and line.startswith("out_time_us="):
                    try:
                        on_time(int(line.split("=", 1)[1]) / 1e6)
                    except ValueError:
                        pass
            rc = p.wait()
        except BaseException:
            # Never leave ffmpeg running (and writing) behind an error.
            p.kill()
            p.wait()
            raise
        err.seek(0)
        stderr = err.read()
    if rc != 0:
        tail = " | ".join(l for l in stderr.strip().splitlines()[-5:] if l.strip())
        raise PickupError(f"ffmpeg failed (rc {rc}): {tail or 'no output'}")
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
        match = jellyfin.jf_movie_lookup(cfg["jellyfin_host"], creds, title, log)
    except Exception as e:
        match = None
        log(f"  could not look up the year for '{title}': {e}", "WARN")
    if not match:
        log(f"  no year found for '{title}' - filing it by label; identify it in Jellyfin", "WARN")
        return movie_item_name(title, None), "label only"
    return movie_item_name(*match), f"label + Jellyfin: {match[0]} ({match[1]})"


def convert_disc_image(cfg, name, log, progress=NULL_PROGRESS):
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
    if not os.path.isdir(park_base) or not common.same_filesystem(cfg["upload_dir"], park_base):
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
        progress.begin(name, "remuxing", best[1], unit="seconds")
        remux_title(iso, best[0], os.path.join(work, item + ".mkv"), on_time=progress.set_done)
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
