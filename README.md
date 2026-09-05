# Foxzilla

A trimmed-down FileZilla, and the verifying pipeline behind it.

Two halves of one path from a disc to a Jellyfin library:

| | |
|---|---|
| **`client/`** | **Foxzilla** — a dual-pane transfer client. SFTP, FTP/FTPS, S3, WebDAV, local disk. No dependencies at all. |
| **`build/`** | macOS `.app` bundler and the icon generator. Also no dependencies. |
| **`pickup/`** | **Foxzilla pickup** — watches the upload drop, verifies every file, hands it to the library, rescans Jellyfin. |

Both are pure Python standard library. No `pip install`, no lockfile, nothing to audit but the source.

## Why this exists

The previous pickup script lost episodes and duplicated files. Three causes, all fixed here and all covered by regression tests named after the bug they reproduce:

- **Half-moved box sets.** A directory was removed in a recursive call and removed again by the caller, raising `FileNotFoundError` partway through a loop over seasons. A set could be reported as moved while most of it was still in the drop folder.
- **`name (1).mkv` on every retry.** A file already at the destination was kept-but-renamed rather than compared. One crash plus one retry produced one duplicate, permanently.
- **A `shutil.rmtree` on the source** of a multi-movie pack, which deleted every file below a size threshold. A DVD-ripped season that missed the filename regex went straight down that path.

## The handoff

Nothing is deleted until the destination has been proven correct.

```
Phase 1  MANIFEST   walk the incoming item; record every file's relative path,
                    size and SHA-256. This is the contract for what must
                    arrive, and it is what makes a missing episode detectable
                    rather than invisible.

Phase 2  STAGE      copy (never move) into a staging dir on the destination
                    filesystem, hashing as we write, and check every file
                    against the manifest. Same filesystem as the source means
                    a hardlink instead: instant, and no extra space.

Phase 3  COMMIT     staging shares a filesystem with the destination, so each
                    file lands by atomic rename. Reconcile against the
                    manifest — and only then remove the source.
```

Invariants:

- **One run at a time**, via `flock`. Overlapping runs were their own bug.
- **Nothing is deleted to make room.** Unclaimed files go to `_leftovers/`, name clashes with different content go to `.quarantine/`.
- **Duplicates are resolved by hash.** Identical means skip; different means quarantine. Never a `(1)` suffix.
- **Any failure leaves the source intact** and the run retryable.

Every handoff writes a JSON receipt with the full manifest, so you can prove after the fact what arrived.

## Credentials

Nobody's password is stored, anywhere.

```
media_pickup.py --login      # authenticate as yourself
media_pickup.py --whoami
media_pickup.py --logout
```

`--login` sends your password to Jellyfin once and keeps only the access token it returns, in a `0600` file. `--api-key` stores a Jellyfin API key instead, and `JELLYFIN_TOKEN` / `JELLYFIN_API_KEY` override both without touching disk. A legacy config containing a password is exchanged for a token on first run and the password erased.

## The client

```
python3 client/foxzilla.py
```

Two panes, a queue, and nothing else.

**The queue is the priority list.** Workers always take the topmost job they are allowed to run, so dragging a row up genuinely promotes it — `⤒ Top`, `↑`, `↓`, `⤓ Bottom`, or `Alt+↑` / `Alt+↓` on a selected row. `Delete` cancels one, and the whole queue pauses and resumes.

**Transfers run in parallel** up to the limit in the toolbar (default 3). Per-backend limits apply on top: FTP keeps a single control connection and stays strictly serial no matter what you set, while SFTP and the HTTP backends open independent ones.

**Skip files already there** compares by size, then by hash when both ends can produce one without transferring the file — the remote side over SSH, which a locked-down sftp-only account won't allow. Where no hash is available it says so on the row rather than pretending the match was exact.

**Progress and rate** come from watching the file's own size: a free local stat for downloads, one cheap `stat` over the multiplexed SSH connection for uploads. OpenSSH's own meter is not used — it draws nothing when driven programmatically, pty or not.

**Interrupted transfers resume** rather than restarting, via `reget` / `reput`.

**SFTP does password auth as well as keys.** Key auth is the default; set `"auth": "password"` on a site and you're prompted at connect. OpenSSH will only take a password from a terminal — never a pipe, and never under `BatchMode` — so those sessions run under a pty, with the batch in a `0600` temp file so stdin stays free for the prompt. The password never appears in an argument or in the process list. Chrooted `internal-sftp` accounts have no shell, so remote hashing and `stat` are skipped for them automatically rather than hanging on a prompt.

### macOS

```
./build/macos/make_app.sh          # -> dist/Foxzilla.app
```

No compilation and nothing to install — the bundle is the script, an icon and a launcher. The launcher prefers the python.org or Homebrew Python, because the Command Line Tools build often ships a Tk too old to render the window properly; if it finds none with Tkinter it says so instead of failing silently.

## Running the pickup

From cron:

```
*/5 * * * * /usr/bin/python3 /opt/foxzilla/media_pickup.py \
    --upload-dir /srv/incoming --movies-dest /srv/movies --shows-dest /srv/tv \
    >> /var/log/foxzilla-pickup.log 2>&1
```

In Docker:

```
docker build -t foxzilla-pickup ./pickup
docker run -d --name foxzilla-pickup --restart unless-stopped \
  -v /srv/incoming:/incoming \
  -v /srv/movies:/movies \
  -v /srv/tv:/tv \
  -v /srv/foxzilla-config:/config \
  -e JELLYFIN_HOST=jellyfin.example:8096 \
  -e TZ=Europe/Dublin \
  foxzilla-pickup

docker exec -it foxzilla-pickup /app/docker-entrypoint.sh --login
```

Each destination must be a single volume — staging lives inside it so the commit can be an atomic rename.

## Tests

```
client/tests/run.sh              # FTP and WebDAV/S3 run against mock servers
                                 # SFTP tests need FOXZILLA_TEST_SSH_HOST set
python3 pickup/tests/test_pickup.py
python3 pickup/tests/test_credentials.py
```

The pickup tests are named for the bugs they reproduce (`bug_1_multiseason_merge_crash`, `bug_2_retry_duplication`, …). The client's FTP, WebDAV and S3 tests spin up their own servers; the S3 mock independently recomputes the SigV4 signature and rejects mismatches.

## Platform

Linux and macOS. The client needs Python 3.9+, Tk, and the `openssh` client for SFTP — all present on a stock Mac. A packaged Mac build is planned.

## Known limits

- S3 and WebDAV uploads buffer in memory — fine to a few hundred MB, not a way to move a disk image.
- S3 signing is verified against an independent implementation in the tests, but has not been exercised against live AWS.
- Skip-if-already-there falls back to a size-only comparison against servers that can't hash remotely (an sftp-only account, S3, WebDAV). The row says when it did.
- Upload progress is sampled about twice a second, so very small files jump straight to 100%.
