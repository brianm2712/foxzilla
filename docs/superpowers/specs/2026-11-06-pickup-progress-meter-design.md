# Pickup progress meter: design

Date: 2026-11-06. Status: approved (approach A).

## Problem

When foxzilla-pickup moves an item from the inbox (`/mnt/incoming/upload`) onto the NAS
(`/mnt/nas-media`), the only feedback is one log line at the start and one at the end.
A 34.7 GB pack (Jiu Jitsu Tutorials, 341 files) shows nothing for its whole run, so it is
impossible to tell whether it is progressing, stuck, or dead. Brian wants to see the
active job and the queue from the main dashboard.

## Scope

In: progress of the active item through every slow phase; the state of everything else
in the inbox; a compact card on `dash.home.prosej.com`.

Out (YAGNI): history beyond the last finished item, notifications, controls (pause,
retry, cancel), a detail page, any change to how items are processed.

## Approach

The pickup writes a `status.json` file; the dashboard fetches it as a static file. This
is the pattern the Health section already uses (`html/health/status.json`). There is no
new service, no new port and no nginx change.

- `run.sh` bind-mounts the host's `/opt/revproxy/html/pickup` into the container as
  `/status`, and sets `STATUS_FILE=/status/status.json`. The entrypoint passes it on as
  `--status-file`. If it is empty, no status is written (tests and dry runs).
- nginx already serves `/opt/revproxy/html` at `dash.home.prosej.com`, so the file is
  at `/pickup/status.json` with no proxy change. The revproxy container is not touched.

Rejected: an HTTP endpoint in the pickup container (needs a second long-running process,
a port and a route), and a host-side observer that infers progress from `du` and the log
(cannot see hashing, and a remux has no known final size).

## Pickup changes (`media_pickup.py`)

### `Progress` reporter

A small class that owns the status document and writes it.

- `Progress(path, clock=time.time)`. If `path` is empty, every method does nothing.
- `queue(items)` replaces the queue list.
- `begin(name, phase, total, files_total=None)` starts a phase for the active item.
- `advance(nbytes, file=None)` adds bytes done. `file_done()` bumps `files_done`.
- `finish(result, error=None)` moves the active item to `last` and clears `active`.
- `idle()` sets `state` to `idle` and writes.
- **Throttle:** writes at most once every 2 s from `advance`/`file_done`. `begin`,
  `finish` and `idle` always write.
- **Rate and ETA:** rate is the bytes done over a rolling window of about 30 s of samples.
  `eta_s = (total - done) / rate`, or `null` until there are 5 s of samples or if the rate is 0.
- **Atomic write:** write to `status.json.tmp` in the same directory, then `os.replace`.
  The file is mode 0644 so nginx can read it.
- **Never fatal:** an `OSError` while writing is logged once as a WARN, and the reporter
  goes quiet until a write succeeds again. Progress reporting must never fail a handoff.

### Hooks into the existing phases

| Phase (`phase` value) | Where | `total` | Advanced by |
|---|---|---|---|
| `hashing` | `build_manifest` → `sha256_file` | item bytes (from `observe`) | each chunk read |
| `copying` | `stage` copy loop | `manifest["total_bytes"]` | each chunk written; `file_done` per file |
| `verifying` | `stage` hardlink path (`sha256_file(dst)`) | `manifest["total_bytes"]` | each chunk read |
| `committing` | `commit` | file count | `file_done` per rename |
| `remuxing` | `remux_title` | title length in seconds (from `probe_titles`) | ffmpeg `out_time_us` |

- `sha256_file` gains an optional `on_chunk(n)` callback. The copy loop calls
  `progress.advance(len(buf), file=entry["path"])`.
- `remux_title` adds `-progress pipe:1 -nostats` and reads stdout line by line. Each
  `out_time_us=` value sets `done` (in seconds) directly. It still raises on a non-zero rc,
  with the stderr tail as today. stderr goes to a temp file so the pipe cannot deadlock.
- The progress object is passed down as a keyword argument that defaults to a null
  `Progress("")`, so existing callers and tests are unchanged.

### Queue and lifecycle in `main`

- A tick that cannot get the lock does **not** touch the status file. The run that holds
  the lock owns it.
- After listing the entries, the run builds the queue from the state it already computes
  for each entry: `uploading` (partial files), `writing` (written to within
  `QUIET_SECONDS`), `stable n/2`, `marked complete`, `held: disc image`,
  `held: convert failed` (with `last_error`), `failed` (with `last_error`), and `waiting`
  for items the loop has not reached yet. Each row has `name`, `bytes` and `files`. The
  active item is not listed in the queue.
- `finish("ok")` runs after a successful `process_item`/`convert_disc_image`, and
  `finish("failed", error)` in each except branch. A failed item then appears in the
  queue with its error on the next write.
- In the `finally`, the run calls `idle()` with the final queue, so the card returns to
  idle when the run ends, even if the run itself crashed.

### `status.json` schema

```json
{
  "version": 1,
  "updated": 1762467540.2,
  "state": "active",
  "active": {
    "name": "Jiu Jitsu Tutorials",
    "phase": "copying",
    "unit": "bytes",
    "done": 12100000000, "total": 34700000000,
    "file": "Ep 112.mp4", "files_done": 118, "files_total": 341,
    "rate": 98000000, "eta_s": 230,
    "phase_started": 1762467300.0, "item_started": 1762466100.0
  },
  "queue": [
    {"name": "Yellowstone Season 5 Mp4 1080p", "bytes": 10700000000, "files": 8, "state": "stable 0/2"},
    {"name": "Foo.iso", "bytes": 7800000000, "files": 1, "state": "held: convert failed", "error": "..."}
  ],
  "last": {"name": "F1 The Movie ...", "result": "ok", "finished": 1762460000.0, "error": null}
}
```

`unit` is `bytes` or `files` (for committing), or `seconds` (for remuxing). The card
formats speed and ETA only for `bytes` and `seconds`.

## Dashboard card

- The live dashboard is `/opt/revproxy/html/index.html` on the PVE host. Back it up to
  `index.html.bak-YYYYMMDD-pickup` first, keep a copy in `~/revproxy-html/`, and put the
  card in the same row as Health. Match the existing card styles.
- It fetches `/pickup/status.json` with `cache: "no-store"` every 5 s, and every 30 s while
  the state is idle.
- **Active:** the item name, then the phase label (Hashing / Copying to NAS / Verifying /
  Committing / Remuxing ISO), a progress bar with %, then `98 MB/s · ETA 3m 50s ·
  file 118/341`. Under it, one line per queue row: name, size, state. Failed and held rows
  are in the warning colour, with the error in the row's tooltip.
- **Idle:** `Idle · last: <name> filed <time>`, or `failed` in the warning colour. Queue
  rows are shown if there are any.
- **Stale:** if `state` is `active` and `updated` is more than 180 s old, show a "stale —
  pickup not reporting" badge and grey out the bar. A frozen 40% must not look healthy.
- **Missing or unreadable file:** `Pickup status unavailable`. It does not break the rest
  of the dashboard.
- Plain JS in the page, the same way Health is done. No build step and no libraries.

## Error handling summary

- The status directory is missing or unwritable: one WARN in `pickup.log`, and the handoff
  carries on. The card shows unavailable or stale.
- The container is killed mid-run: the file is left `active` with an old `updated`, so the
  card shows stale. The next run rewrites it.
- ffmpeg progress output can't be parsed: the bar stays at its last value, and the remux
  result is unaffected.

## Testing

- New unit tests in `tests/test_pickup.py`:
  - the throttle (with a fake clock: one write per 2 s, while `begin`/`finish` always write)
  - rate and ETA (window, `null` before 5 s of samples and when the rate is 0)
  - the atomic write (no `.tmp` left behind; the file is valid JSON)
  - an unwritable path (one WARN, no exception)
  - an empty path (no file)
  - `stage` with a recording `Progress` ends at `done == total` and `files_done == files_total`
  - `remux_title` parses a canned `-progress` stream, via a fake ffmpeg script
  - `main` with a temp inbox writes a queue with the expected states and ends `idle`
  - a run that can't get the lock leaves the file untouched
- All 101 existing tests still pass.
- Card: check it in the browser against canned `active`, `idle`, `stale` and missing files,
  then against a real item.

## Deploy

1. Check the host's md5 against the repo.
2. Copy the files to `/root/foxzilla-build`, run the tests there, and `docker build`.
3. `mkdir -p /opt/revproxy/html/pickup`.
4. `./run.sh`, **only when the pickup is idle** (`run.sh` kills an in-flight handoff).
5. Edit the dashboard with a backup.
6. Watch the next real item.
