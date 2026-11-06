"""Watcher state, the dashboard queue rows, and main()."""
from __future__ import annotations

import argparse
import json
import os
import time

# process_item and cmd_whoami are called through their modules (handoff.,
# jellyfin.) so tests patch them in one place.
from . import handoff, jellyfin
from .common import (DEFAULTS, DISC_IMAGE_EXTS, QUIET_SECONDS, STABLE_POLLS, Log, PickupError,
                     acquire_lock, human, is_junk, marker_for, observe, sweep_markers)
from .disc import NAME_SUFFIX, convert_disc_image, ffmpeg_available, name_file_for
from .handoff import report_staging
from .jellyfin import cmd_login, cmd_logout, jf_rescan, load_credentials
from .progress import Progress, cmd_health


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


def queue_row(name, now, state, error=None):
    """One inbox entry as the dashboard's queue shows it."""
    row = {"name": name, "bytes": now["bytes"] if now else None,
           "files": now["files"] if now else None, "state": state}
    if error:
        row["error"] = error
    return row


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
    ap.add_argument("--health", action="store_true",
                    help="container healthcheck: exit 0 if status.json shows a live watcher")
    args = ap.parse_args(argv)
    cfg = build_config(args)

    # --api-key is a credential action in its own right; requiring --login
    # alongside it meant it silently fell through to a normal watcher run.
    if args.login or args.api_key:
        return cmd_login(cfg["jellyfin_host"], args.login_user, args.api_key)
    if args.logout:
        return cmd_logout()
    if args.whoami:
        return jellyfin.cmd_whoami()
    if args.health:
        # The healthcheck runs outside the entrypoint, so --status-file is
        # usually absent; the container's STATUS_FILE env is the fallback.
        return cmd_health(cfg["status_file"] or os.environ.get("STATUS_FILE", ""))

    log = Log(cfg["log_file"])

    if not os.path.isdir(cfg["upload_dir"]):
        log(f"upload dir {cfg['upload_dir']} missing", "ERROR")
        return 1

    lock = acquire_lock(cfg["lock_file"])
    if lock is None:
        log("another run is still working - skipping this tick")
        return 0

    # Only the run holding the lock may touch the status file: a skipped tick
    # writing "idle" would clobber the live run's progress.
    progress = Progress("" if args.dry_run else cfg["status_file"], log=log)
    rows = {}

    try:
        entries = [e for e in sorted(os.listdir(cfg["upload_dir"]))
                   if not e.startswith(".") and not is_junk(e)
                   and not e.endswith(NAME_SUFFIX)]
        rows.update((n, queue_row(n, None, "waiting")) for n in entries)
        report_staging(cfg, log, progress, dry_run=args.dry_run)
        if not entries:
            # Still tidy up: a marker whose item never arrived, or was already
            # dealt with, would otherwise sit there indefinitely.
            sweep_markers(cfg["upload_dir"], entries)
            return 0

        state = load_state(cfg["state_file"])
        processed = []
        # One item per run. A run holds the lock for as long as it works, so
        # filing a whole backlog in one go (12 large items took hours) left
        # new arrivals unobserved, the dashboard queue stale, and a deploy
        # with no choice but to kill a copy mid-flight. Once one item has
        # been attempted, the rest are still observed - state, rows and logs
        # stay current - but only marked ready for the next run. A disc
        # conversion and the filing of its MKV are one item together.
        attempted = False
        converted = None
        # Never-failed items get the slot first. In plain sorted order a
        # permanently failing item early in the listing took the slot every
        # time it was ready and starved everything after it (two failing
        # items simply took turns). `entries` itself stays the full listing:
        # it is what pruning and the marker sweep go by.
        order = sorted(entries, key=lambda n: (bool(state.get(n, {}).get("last_error")), n))

        for name in order:
            path = os.path.join(cfg["upload_dir"], name)
            now = observe(path)
            prev = state.get(name, {})
            err = prev.get("last_error")

            def remember(entry, err=err):
                # A failure is remembered until the item succeeds, through
                # every waiting tick: the ordering above depends on it.
                if err:
                    entry["last_error"] = err
                return entry

            marker = marker_for(cfg["upload_dir"], name)
            declared = os.path.exists(marker)
            # A marker skips the waits only until the item fails. After that
            # it backs off like any other item, or a permanently failing
            # marked item would be re-hashed and retried every tick. The
            # marker itself stays until the item succeeds.
            declared_fast = declared and not err

            def say(msg, prev=prev):
                # A waiting item used to log the same line every tick (about
                # 4,100 a day). Log only when the message changes; the caller
                # stores what was said in the entry's state as "said".
                if msg != prev.get("said"):
                    log(msg)
                return msg

            if not now["files"] and not now["partial"]:
                # Nothing usable (an empty folder, or only junk like
                # .DS_Store). Processing it can only fail, and it would fail
                # every tick forever. Hold it instead: the shape changes when
                # files arrive, and then it is no longer empty. A marker does
                # not change that - there is still nothing to file.
                if prev.get("held") != "empty":
                    log(f"holding {name}: no usable files (empty folder) - "
                        f"it will be picked up when files appear", "WARN")
                state[name] = remember({"shape": now["shape"], "stable": 0, "held": "empty"})
                rows[name] = queue_row(name, now, "held: empty")
                continue

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
                state[name] = remember({"shape": now["shape"], "stable": 0, "held": "disc image"})
                rows[name] = queue_row(name, now, "held: disc image")
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
                    rows[name] = queue_row(name, now, "held: convert failed", err)
                    continue
            if now["partial"] and not declared_fast:
                said = say(f"watching {name}: transfer still in progress (partial files)")
                state[name] = remember({"shape": now["shape"], "stable": 0, "said": said})
                rows[name] = queue_row(name, now, "uploading", err)
                continue
            # A declared item (marker present, no failure yet) skips the quiet
            # and stability waits: see below where it is acted on.
            if not declared_fast and time.time() - now["newest"] < QUIET_SECONDS and not args.force:
                said = say(f"watching {name}: written to in the last {QUIET_SECONDS}s")
                state[name] = remember({"shape": now["shape"], "stable": 0, "said": said})
                rows[name] = queue_row(name, now, "writing", err)
                continue

            stable = prev.get("stable", 0) + 1 if prev.get("shape") == now["shape"] else 0
            if stable < STABLE_POLLS and not args.force and not declared_fast:
                said = say(f"watching {name}: {now['files']} files, {human(now['bytes'])} "
                           f"(stable {stable}/{STABLE_POLLS})")
                state[name] = remember({"shape": now["shape"], "stable": stable, "said": said})
                rows[name] = queue_row(name, now, f"stable {stable}/{STABLE_POLLS}", err)
                continue

            if attempted and name != converted:
                # Ready, but this run has had its item. Keep the stability
                # count (and the marker) so the next run acts without
                # re-waiting.
                said = say(f"next run: {name}")
                state[name] = remember({"shape": now["shape"], "stable": stable, "said": said})
                rows[name] = queue_row(name, now, "ready (next run)", err)
                continue

            if declared_fast:
                # The uploader has verified this item and said so. Trusting
                # that is safe: the manifest is rebuilt and every file is
                # hashed again before anything is deleted, so a marker written
                # too early costs a failed run, never data.
                log(f"{name}: upload marked complete by the sender")

            if convertible:
                if args.dry_run:
                    log(f"would convert {name} ({human(now['bytes'])} disc image)")
                    rows[name] = queue_row(name, now, "waiting", err)
                    continue
                log(f"converting {name} ({human(now['bytes'])} disc image)")
                attempted = True
                progress.queue(list(rows.values()))
                try:
                    item = convert_disc_image(cfg, name, log, progress=progress)
                    state.pop(name, None)
                    progress.finish(name, "converted")
                    rows.pop(name, None)
                    rows[item] = queue_row(item, None, "waiting")
                    # The new item joins this tick's listing: the loop reaches
                    # it next (its marker lets it skip the stability wait, so
                    # it is filed straight away) and its marker is not swept.
                    entries.append(item)
                    order.append(item)
                    converted = item
                except PickupError as e:
                    log(f"  convert FAILED, disc left in the inbox: {e}", "ERROR")
                    state[name] = {"shape": now["shape"], "stable": 0,
                                   "held": "convert failed", "last_error": str(e)}
                    rows[name] = queue_row(name, now, "failed", str(e))
                    progress.finish(name, "failed", str(e))
                except Exception as e:
                    log(f"  convert FAILED unexpectedly, disc left in the inbox: "
                        f"{type(e).__name__}: {e}", "ERROR")
                    state[name] = {"shape": now["shape"], "stable": 0,
                                   "held": "convert failed", "last_error": f"{type(e).__name__}: {e}"}
                    rows[name] = queue_row(name, now, "failed", f"{type(e).__name__}: {e}")
                    progress.finish(name, "failed", f"{type(e).__name__}: {e}")
                continue

            log(f"processing {name} ({now['files']} files, {human(now['bytes'])})")
            if not args.dry_run:
                attempted = True
            progress.queue(list(rows.values()))
            try:
                dests = handoff.process_item(cfg, name, log, dry_run=args.dry_run, progress=progress)
                processed.extend(dests)
                state.pop(name, None)
                if not args.dry_run:
                    progress.finish(name, "ok")
                    rows.pop(name, None)
                    try:
                        os.remove(marker)
                    except OSError:
                        pass
            except PickupError as e:
                log(f"  FAILED, source left intact: {e}", "ERROR")
                state[name] = {"shape": now["shape"], "stable": 0,
                               "last_error": str(e)}
                rows[name] = queue_row(name, now, "failed", str(e))
                progress.finish(name, "failed", str(e))
            except Exception as e:
                log(f"  UNEXPECTED FAILURE, source left intact: "
                    f"{type(e).__name__}: {e}", "ERROR")
                state[name] = {"shape": now["shape"], "stable": 0,
                               "last_error": f"{type(e).__name__}: {e}"}
                rows[name] = queue_row(name, now, "failed", f"{type(e).__name__}: {e}")
                progress.finish(name, "failed", f"{type(e).__name__}: {e}")

        for name in list(state):
            if name not in entries:
                state.pop(name)
        sweep_markers(cfg["upload_dir"], entries)
        if not args.dry_run:
            # A dry run touches nothing: saving its state (and the "said"
            # lines in it) would silence the next real run's log lines.
            save_state(cfg["state_file"], state)

        if processed and not args.dry_run:
            jf_rescan(cfg["jellyfin_host"],
                      load_credentials(cfg["jellyfin_host"], log), log)
        return 0
    finally:
        # Every run ends idle, even a crash: a stale "active" would claim a
        # handoff is still running.
        progress.queue(list(rows.values()))
        progress.idle()
        lock.close()
