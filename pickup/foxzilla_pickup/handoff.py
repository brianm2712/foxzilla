"""The three-phase verified handoff: manifest, stage, organize, commit, reconcile."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import time

# sha256_file / same_filesystem / free_bytes are called as common.<name> so a
# test patches them in one place (common).
from . import common
from .common import CHUNK, VIDEO_EXTS, PickupError, human, prune_empty, route_for, walk_files
from .classify import classify, guess_show_name, movie_item_name, video_title_year
from .progress import NULL_PROGRESS


# A candidate main feature must be within 40% of the largest video AND at
# least this big, so a 150 MB sample never gets promoted to a feature.
MIN_FEATURE_BYTES = 200 * 1024 * 1024
FEATURE_RATIO = 0.4


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
            "sha256": common.sha256_file(full, on_chunk=lambda n, rel=rel: progress.advance(n, rel)),
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


# --------------------------------------------------------------------------
# phase 2: staging
# --------------------------------------------------------------------------


def stage(src_root, manifest, staging, log, dry_run=False, progress=NULL_PROGRESS):
    """
    Phase 2. Copy into staging on the destination filesystem and verify.

    Where the source and staging share a filesystem we hardlink instead of
    copying: instant, no extra space (which matters - / is nearly full), and
    the content is identical by construction. Otherwise we stream a copy and
    hash it as it is written, so verification costs no extra read.
    """
    os.makedirs(staging, exist_ok=True)
    linking = common.same_filesystem(src_root, staging)
    log(f"  phase 2: staging into {staging} ({'hardlink' if linking else 'copy'})")
    if not dry_run:
        progress.begin(os.path.basename(src_root.rstrip("/")),
                       "verifying" if linking else "copying",
                       manifest["total_bytes"], files_total=len(manifest["files"]))

    if not linking:
        need = manifest["total_bytes"]
        avail = common.free_bytes(os.path.dirname(staging))
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
            got = common.sha256_file(dst, on_chunk=lambda n, p=entry["path"]: progress.advance(n, p))

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


# Files that belong to a video and travel with it when a pack is split:
# "Film.srt", "Film.en.srt", "Film.idx" + "Film.sub" beside "Film.mp4".
SIDECAR_EXTS = {".srt", ".sub", ".idx", ".ass", ".ssa", ".vtt", ".nfo"}


def sidecars_of(staging, video_rel):
    stem = os.path.splitext(os.path.basename(video_rel))[0]
    folder = os.path.dirname(video_rel)
    try:
        names = os.listdir(os.path.join(staging, folder))
    except OSError:
        return []
    return [os.path.join(folder, n) for n in sorted(names)
            if os.path.splitext(n)[1].lower() in SIDECAR_EXTS
            and (os.path.splitext(n)[0] == stem or n.startswith(stem + "."))]


def organize_movie_staging(staging, title, log, renames=None, pack=False):
    """
    Tidy a movie inside staging: promote the main feature, push the rest into
    extras/. Multi-movie packs are split into sibling folders.

    Unlike v1 this never deletes anything: whatever is not claimed by a movie
    goes to _leftovers/ for a human to look at.

    Every file moved is recorded in `renames` as
    {staged rel path: (target title, rel path inside that target)}, so
    reconciliation can follow it. Without that, a renamed feature looks
    missing and every movie with extras fails its own handoff.

    `pack` (the classifier said "film pack") makes every video of feature
    size a film of its own, whatever its size relative to the largest: in a
    box set a 720p film is a third the size of a 1080p one, and the 40% rule
    would have filed it as an extra. Each film's folder is "Title (Year)"
    from its name or folder, and its subtitles go with it.
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
    if pack:
        large = [v for v in ordered if sizes[v] >= MIN_FEATURE_BYTES]

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
            key = os.path.splitext(os.path.basename(v))[0]
            if pack:
                t, y = video_title_year(v)
                if y:
                    key = movie_item_name(t, y)
            features.setdefault(key, v)
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
            if pack:
                for sc in sidecars_of(staging, v):
                    os.rename(os.path.join(staging, sc), os.path.join(folder, os.path.basename(sc)))
                    renames[sc] = (movie_title, os.path.basename(sc))
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
                    and common.sha256_file(target, on_chunk=lambda n: progress.touch()) \
                    == common.sha256_file(full, on_chunk=lambda n: progress.touch()):
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
# processing one item
# --------------------------------------------------------------------------


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
    # An item inside a sorting folder (Movies/, TV Shows/, Videos/, BJJ/) is
    # filed where the folder says; only a top-level item is guessed at.
    base = os.path.basename(name)
    route = route_for(name)
    if route:
        kind, sub = route
        why = f"uploaded into {name.split('/', 1)[0]}/"
    else:
        (kind, why), sub = classify(base, rel_paths, sizes), ""
    log(f"  classified as {kind.upper()} ({why})")
    is_file = os.path.isfile(src)

    if kind == "tv":
        lib_root = cfg["shows_dest"]
        title = guess_show_name(base) if is_file else base
    elif kind == "video":
        lib_root = cfg.get("videos_dest") or ""
        if not lib_root:
            raise PickupError("this is a Videos item but no Videos library is set "
                              "(--videos-dest / VIDEOS_DEST); left in the inbox")
        title = os.path.splitext(base)[0] if is_file else base
    else:
        lib_root = cfg["movies_dest"]
        title = os.path.splitext(base)[0] if is_file else base
    # A sorting folder can name a collection inside the library
    # (BJJ/ -> Videos/Jiu Jitsu Tutorials/<course>). Staging stays at the
    # library root, beside every other hidden working dir.
    dest_root = os.path.join(lib_root, sub) if sub else lib_root

    staging_base = os.path.join(lib_root, ".staging")
    quarantine = os.path.join(lib_root, ".quarantine")
    os.makedirs(staging_base, exist_ok=True)
    if not dry_run:
        os.makedirs(dest_root, exist_ok=True)
    staging = os.path.join(staging_base, f"{os.getpid()}-{int(started)}")

    placements, skipped, conflicts = {}, [], []
    renames = {}
    try:
        stage(src, manifest, staging, log, dry_run=dry_run, progress=progress)

        if kind == "movie" and not dry_run:
            targets = organize_movie_staging(staging, title, log, renames,
                                             pack=why.startswith("film pack"))
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
# abandoned staging (reported, never deleted)
# --------------------------------------------------------------------------

STALE_STAGING_SECONDS = 86400   # a staging dir untouched this long is abandoned


def abandoned_staging(cfg, now=None, cache=None):
    """
    Staging dirs a failed run left "for inspection" and nobody came back to.
    Reported only: leftovers are for a human, never deleted to make room.

    cache, if given, is {path: {"mtime": .., "bytes": ..}} from earlier runs:
    a dir whose mtime is unchanged is not walked again (the walk is over CIFS,
    every tick). It is updated in place to hold just the dirs found now that
    were measured successfully.
    """
    now = time.time() if now is None else now
    old_cache = dict(cache or {})
    if cache is not None:
        cache.clear()
    seen, found = set(), []
    for root in (cfg["movies_dest"], cfg["shows_dest"]):
        base = os.path.join(root, ".staging")
        key = os.path.realpath(base)
        if key in seen or not os.path.isdir(base):
            continue
        seen.add(key)
        for name in sorted(os.listdir(base)):
            path = os.path.join(base, name)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if not stat.S_ISDIR(st.st_mode) or now - st.st_mtime < STALE_STAGING_SECONDS:
                continue
            hit = old_cache.get(path)
            if isinstance(hit, dict) and hit.get("mtime") == st.st_mtime \
                    and isinstance(hit.get("bytes"), int):
                total, measured = hit["bytes"], True
            else:
                total, measured = 0, True
                try:
                    for dp, _dn, fns in os.walk(path):
                        for f in fns:
                            try:
                                total += os.lstat(os.path.join(dp, f)).st_size
                            except OSError:
                                pass
                except OSError:
                    measured = False    # not cached: measure again next run
            if cache is not None and measured:
                cache[path] = {"mtime": st.st_mtime, "bytes": total}
            found.append({"path": path, "bytes": total,
                          "age_days": round((now - st.st_mtime) / 86400, 1)})
    return found


def staging_seen_file(cfg):
    # Beside state.json, not in it: main() prunes state keys that are not
    # inbox entries, and an inbox item may be called anything.
    return cfg["state_file"] + ".staging.json"


def report_staging(cfg, log, progress, dry_run=False):
    """Publish abandoned staging; WARN only when the set changes. Never raises."""
    try:
        seen_file = staging_seen_file(cfg)
        try:
            with open(seen_file) as fh:
                saved = json.load(fh)
        except (OSError, ValueError):
            saved = []
        # Older runs saved just the list of paths; now {"paths", "cache"}.
        if isinstance(saved, dict):
            before = saved.get("paths") or []
            old_cache = saved.get("cache") if isinstance(saved.get("cache"), dict) else {}
        else:
            before = saved if isinstance(saved, list) else []
            old_cache = {}
        cache = dict(old_cache)
        found = abandoned_staging(cfg, cache=cache)
        progress.staging(found)
        paths = sorted(r["path"] for r in found)
        if paths != before:
            if found:
                total = sum(r["bytes"] for r in found)
                listing = ", ".join(f"{r['path']} ({r['age_days']}d)" for r in found)
                log(f"abandoned staging (not deleted): {len(found)} dir(s), "
                    f"{human(total)} - {listing}", "WARN")
        if not dry_run and (paths != before or cache != old_cache or not isinstance(saved, dict)):
            tmp = seen_file + ".tmp"
            with open(tmp, "w") as fh:
                json.dump({"paths": paths, "cache": cache}, fh)
            os.replace(tmp, seen_file)
    except Exception as e:
        try:
            log(f"could not check abandoned staging: {type(e).__name__}: {e}", "WARN")
        except Exception:
            pass
