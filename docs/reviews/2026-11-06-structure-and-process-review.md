# Foxzilla: file structure and process-handling review

Date: 2026-11-06. Scope: `pickup/`, `client/`, `web/`, `build/`, and how they are deployed on the PVE host.
Each recommendation can be approved or denied on its own. **Nothing here has been changed yet.**

Live facts behind this (checked 2026-11-06):
- `foxzilla-pickup` container: up 11 days. Its log is 1.4 MB / 15.6k lines and has never been rotated.
  - It contains **3,230 "item contains no usable files" errors**.
  - About **17 GB of abandoned staging** sits in `/mnt/nas-media/tv/.staging`:
    - `18220-1793003158`: six Harry Potter films, 15 GB, from 2026-10-26;
    - `321-1790505705`: 2 GB.
  - `movies/.quarantine` holds 4.4 GB (Toy Story 3).
- `foxzilla-web.service`: running on the host as `fenris`, on `127.0.0.1:8111`.
  - **No current route reaches it.** Only the September `prosej.yml.bak-*` tunnel configs mention it.
  - Its code in `/opt/fenris/foxzilla` is not a git checkout. `client/foxzilla.py` there **differs from the repo**.
- Tests:
  - pickup: 166 pass.
  - client (offline/loopback): scan 19, queue_priority 19, ftp 17, dav_s3 25, sftp_password 26, all passing.
  - web egress: 37/39 pass; the 2 failures need public DNS.
  - `client/tests/run.sh` is broken on macOS (`timeout` is missing there).

---

## A. Pickup (inbox → NAS)

**A1. Hold empty items instead of retrying them forever.** (S)
- **Problem:** Empty folders in the inbox fail every few minutes, forever. Examples: The Northman, The Simpsons, The Sopranos, and about 15 others. That is 3,230 ERROR lines so far, and they bury real failures.
- **Fix:** Treat them like held disc images: log once, show `held: empty` on the card, and retry only when the folder changes.

**A2. Report abandoned staging; don't silently keep it.** (S)
- **Problem:** A failed or killed run leaves `<dest>/.staging/<pid>-<ts>`, and nothing ever looks at it again. There is about 17 GB of it now.
- **Fix:** At the start of each run, list any staging directory older than a day. Log it once and show it on the card. Deleting stays manual, by the "never delete to make room" rule.
- *Separate approval:* whether to remove the two existing directories (the Harry Potter films are probably already in Movies; I'd check by hash first).

**A3. File one item per run instead of the whole inbox.** (M)
- **Problem:** Today a single run holds the lock until every queued item is filed. Right now that is Jiu Jitsu plus about 12 more items, which is hours. Meanwhile:
  - new arrivals aren't even looked at;
  - the queue goes stale;
  - any restart or deploy has to wait for the whole backlog, or kill a copy halfway.
- **Fix:** Process one item, then end the run. The next tick (60 s later) takes the next one.
- **Cost:** about a 60 s gap between items, and one Jellyfin rescan per item instead of per batch.

**A4. Rotate the pickup log and stop logging the same "watching" line every minute.** (S)
- **Fix:** Add a host `logrotate` rule for `/srv/foxzilla-config/pickup.log` (weekly, keep 8, compress). Only log "watching X" when X's state changes. Today it is about 4.1k identical lines.

**A5. Make the container healthcheck mean "the watcher is alive".** (S, needs the progress meter)
- **Problem:** The healthcheck runs `--whoami`, which only proves a Jellyfin credential exists. A run hung on the CIFS NAS mount still shows "healthy".
- **Fix:** The healthcheck reads `status.json`. It is unhealthy if `updated` is more than 10 minutes old while active, or more than 5 minutes old while idle.
- Optionally, pve-health alerts to ntfy on that.

**A6. Deploy from git, not by copying files.** (S–M)
- **Problem:** Both `/root/foxzilla-build` (pickup) and `/opt/fenris/foxzilla` (web) are copied-in snapshots. The md5 check before each deploy exists only because of that, and the web copy has already drifted.
- **Fix:** A `deploy.sh` in the repo that:
  - refuses to run with a dirty tree or unpushed commits;
  - copies a `git archive` of a commit;
  - stamps the commit SHA into the image label / a `VERSION` file;
  - runs the tests on the host;
  - and, for the pickup, waits until the pickup is idle before recreating the container.

**A7. Split `media_pickup.py` (~1,700 lines) into a small package.** (M, no behaviour change)
- **Modules:** `handoff.py` (manifest/stage/commit/reconcile), `classify.py`, `disc.py` (ISO/ffmpeg), `jellyfin.py`, `credentials.py`, `progress.py`, `cli.py` (main loop).
- The Dockerfile copies the package. The tests keep importing the same names through a shim.

## B. Client (`client/foxzilla.py`, 2,749 lines)

**B1. Stop path traversal from server-supplied names.** (S)
- **Problem:** Names from FTP/S3/SFTP listings are joined into local paths without checks (`Queue._expand` `:1559`, `plan_transfer` `:1709`). A hostile FTP server or shared S3 bucket that lists `../x` or `/Users/brianmurphy/.zshrc` writes outside the chosen folder during a recursive download.
- **Fix:** One `safe_name()` check on every listing entry, plus a check that each destination stays under the chosen root.

**B2. Make Cancel and Quit really stop transfers; download to `.part`.** (M)
- **Problem:**
  - An SFTP cancel frees the worker slot but leaves `sftp` running (`:676`, `timeout=None`).
  - FTP never calls `abort()`.
  - S3/WebDAV can't be cancelled at all.
  - Quit gives no warning.
  - Downloads write straight to the final name.
  - The README's "resume via reget/reput" is dead code: `resume=True` is never passed.
- **Fix:**
  - Run SFTP through `Popen` per job and kill it on cancel; call `ftp.abort()` and reconnect.
  - Download to `name.part`, then rename.
  - Confirm on Quit.
  - Either wire up resume or remove the README claim.

**B3. Write the drop `.complete` marker only when that item actually finished.** (M)
- **Problem:** Markers are armed before the scan (`:2433`) and written whenever the *whole queue* goes idle (`:2546`). Cancelled jobs don't block them.
- **Why it matters for the pickup:** a marker tells it to skip the stability wait. A premature marker makes it file a partial item, for example half a season. That's not data loss, but it gives you split imports.
- **Fix:** A per-item batch that owns its jobs. Write its marker only when all of them are done and none failed or were cancelled. Arm it after the files are queued.

**B4. Don't share one connection between browsing and transfers; lock the queue.** (S–M)
- **Problem:** Browsing an FTP pane during an FTP transfer interleaves commands on one control connection. `clear_finished` changes `queue.jobs` without the lock.
- **Fix:** Give the queue its own backend per site and add a locked `remove_finished()`.

**B5. Verify the FTPS certificate.** (S)
- **Problem:** `FTP_TLS()` has no context (`:839`), so there's no certificate check and the FTPS password can be intercepted.
- **Fix:** Use `ssl.create_default_context()`, with a per-site `ca_file` or an explicit `insecure_tls` opt-out for self-signed homelab boxes.

**B6. SFTP keepalives and a stall watchdog.** (S)
- **Problem:** With no `ServerAliveInterval`, a dropped connection hangs a worker forever.
- **Fix:** Add `ServerAliveInterval=15`, `ServerAliveCountMax=4`, and kill the job after N minutes with no progress.

**B7. Split `client/foxzilla.py` into a package.** (M, no behaviour change)
- **Layout:** `backends/{local,sftp,ftp,s3,webdav}.py`, `queue.py`, `scan.py`, `config.py`, `gui.py`, `cli.py`.
- Keep `client/foxzilla.py` as a shim. `web/` then imports the core without Tk.
- Best done **before** B1–B6, so those fixes land in small files.

**B8. Secrets.** (S–M)
- **Problem:**
  - Passwords sit in plaintext in `secrets.json` (0600) and S3 keys in `sites.json`.
  - `save_secret` is never called, so "remember password" doesn't exist despite the docstring.
- **Fix:** Use the macOS Keychain via `/usr/bin/security`, with the file as a fallback.

## C. Hosted web (`web/`, `foxzilla-web.service`)

These are **not currently urgent** because no route reaches the service. They must be fixed before it is ever exposed again.

**C0. Decide its fate:** either stop and disable `foxzilla-web.service` (it's running but unreachable), or keep it and fix C1–C3 first. (S)

**C1. Shared SSH connection between visitors.** (S)
- **Problem:** The control socket is `/tmp/.foxzilla-<uid>-%r@%h-%p` (`client/foxzilla.py:392`, `ControlPersist=120`), and `web/server.py:181` doesn't override it. A second visitor to the same user@host within 2 minutes rides the first visitor's authenticated connection **with any password**.
- **Fix:** A private socket directory per session, or `ControlMaster=no` on the web server.

**C2. WebDAV can get around the egress guard.** (M)
- **Problem:** `server.py:162` checks the address but passes the hostname on, and urllib follows redirects. A visitor's server can redirect to 169.254.169.254, the LAN, or the tailnet.
- **Fix:** Refuse redirects, and connect to the checked IP.

**C3. Input hardening.** (S)
- **Problem and fix:**
  - A truncated upload is accepted as complete (`server.py:469-476`): fail with 400 instead.
  - The SFTP user is passed straight into the arguments: reject a leading `-` and whitespace.
  - `Content-Disposition` uses the raw remote name: sanitise it.
  - Temp files use fixed `/tmp` paths: use `mkstemp`.
  - `reap()` runs `ssh -O exit` while holding the global lock: close outside the lock.
  - Add a per-IP session cap.

## D. Repo and tests

**D1. One test runner that actually fails.** (S)
- **Problem:**
  - `client/tests/run.sh` needs `timeout` (missing on macOS), pipes through `tail` (which loses the exit code), and skips 3 of the 8 files.
  - Five client test files never exit non-zero.
- **Fix:** Add `tests/run_all.py` (stdlib only, per-file timeout, non-zero on any failure) and `sys.exit(1)` on failure in each file. Stub DNS in the two egress tests.

**D2. Tidy the repo.** (S)
- Add `dist/`, `.DS_Store` and `.claude/` to `.gitignore`. `dist/` is a 208 KB built `.app` with a copy of the source that will drift.
- Delete the untracked `.DS_Store` files.
- Move `docs/sites.talos.json` (your personal site list) out of the repo if it's ever mirrored.
- Quote `$ROOT` in `build/linux/install.sh`.

---

## Suggested order if most are approved

1. Quick, safe pickup wins: A1, A4, A2.
2. Deploy and health: A6 (with the progress meter), then A5.
3. Decide C0.
4. Client: B7 (split), then B1, B5, B6, B2, B3, B4.
5. D1 alongside, then A3, A7, B8, D2.
