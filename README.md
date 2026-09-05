# Foxzilla

A trimmed-down FileZilla, and the verifying pipeline behind it.

Two halves of one path from a disc to a Jellyfin library:

| | |
|---|---|
| **`client/`** | **Foxzilla** — a dual-pane transfer client. SFTP, FTP/FTPS, S3, WebDAV, local disk. No dependencies at all. |
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

## Running

Client:

```
python3 client/foxzilla.py
```

Pickup, from cron:

```
*/5 * * * * /usr/bin/python3 /opt/foxzilla/media_pickup.py \
    --upload-dir /srv/incoming --movies-dest /srv/movies --shows-dest /srv/tv \
    >> /var/log/foxzilla-pickup.log 2>&1
```

Pickup, in Docker:

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

- SFTP shows no byte-level progress: the OpenSSH client only draws its meter to a terminal. Every other backend reports real progress.
- S3 and WebDAV uploads buffer in memory — fine to a few hundred MB, not a way to move a disk image.
- S3 signing is verified against an independent implementation in the tests, but has not been exercised against live AWS.
