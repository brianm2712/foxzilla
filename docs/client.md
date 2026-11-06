# Foxzilla client

A trimmed-down FileZilla: two panes, a transfer queue, and nothing else.

    python3 foxzilla.py

## Why it has no dependencies

Every protocol is either stdlib or shells out to a tool that is already
installed, so there is nothing to install and nothing to audit but the source:

| Protocol   | Implementation                | Notes |
|------------|-------------------------------|-------|
| SFTP       | the OpenSSH `sftp` binary     | inherits your keys, ssh-agent, jump hosts and `~/.ssh/config` aliases |
| FTP / FTPS | stdlib `ftplib`               | MLSD where offered, `LIST` parsing as fallback |
| S3         | hand-rolled SigV4 + `urllib`  | path-style addressing, so MinIO / R2 / Backblaze / Wasabi work too |
| WebDAV     | stdlib `http.client` + ElementTree | Nextcloud-style `PROPFIND`/`MKCOL`/`MOVE` |
| Local disk | `os` / `shutil`               | a local "site" can pin a start folder |

Copy the `client/` folder (`foxzilla.py` plus the `foxzilla/` package beside it)
to any machine with Python 3.9+ and Tk and it runs — Linux or macOS. Nothing to
install. `python3 -m foxzilla` from inside `client/` works too.

Layout: `foxzilla/backends/` (one module per protocol), `queue.py` (the transfer
queue and per-item batches), `scan.py` (the pre-flight scan), `config.py` (sites
and secrets), `gui.py` (the Tk window, imported only when needed), `cli.py`.

## Sites

Saved in `~/.config/foxzilla/sites.json` (mode 0600). Edit via the **Sites**
menu, or by hand:

```json
[
  {"name": "Local",   "type": "local"},
  {"name": "Backup",  "type": "local", "path": "/media/backup-drive"},
  {"name": "server",  "type": "sftp",  "host": "myserver"},
  {"name": "backups", "type": "s3",    "bucket": "my-bucket", "region": "eu-west-1",
                      "endpoint": "https://minio.example:9000",
                      "access_key": "…", "secret_key": "…"},
  {"name": "cloud",   "type": "dav",   "url": "https://host/remote.php/dav/files/me", "user": "me"}
]
```

The `sftp` type takes a `~/.ssh/config` alias in `host`, which is why the
server entry can be a bare alias — no user, port or key needed.

**Secrets stay out of `sites.json`.** Passwords are prompted for at connect; tick
*Remember* and, once the connection has worked, the password is saved in the
**macOS Keychain** (service `foxzilla`, account = site name), handed to
`/usr/bin/security` on stdin so it never appears in a process list. S3 keys typed
into a site, or found in an existing `sites.json`, are moved there too. Where
there is no keychain (Linux), or it refuses, the fallback is
`~/.config/foxzilla/secrets.json` at 0600 — plain JSON, obfuscation, not
encryption. *Sites › Forget saved password* removes one; removing a site forgets
its secrets. S3 credentials still fall back to `AWS_ACCESS_KEY_ID` /
`AWS_SECRET_ACCESS_KEY` when nothing is stored.

**FTPS checks the server certificate.** For a self-signed homelab server either
set `"ca_file"` to its certificate (or CA) or, knowingly, `"insecure_tls": true`.

**Drop folders** (`"drop": true` on a site) get a `.<name>.complete` marker per
item, written only once every file of that item has arrived and none failed or
was cancelled.

## The queue

Order is priority — the workers take the topmost job they're allowed to run. `⤒ Top`, `↑`, `↓`, `⤓ Bottom`, or `Alt+↑`/`Alt+↓` on a row; `Delete` cancels. Pause and resume affect the whole queue.

Transfers run in parallel up to the toolbar limit. FTP is pinned to one at a time regardless (single control connection); other backends open independent ones.

## Using it

Pick a site per pane, hit **Connect**. Double-click to descend, `↑` for the
parent, `⌂` for home. Select files and hit **→ Send** / **← Fetch**, or use the
right-click menu. Folders transfer recursively. Two remote panes work as well:
the file is relayed through a temp file on this machine.

`F2` renames, `Del` deletes, right-click makes a new folder.

## Known limits

- SFTP shows no byte-level progress — the OpenSSH client only draws its meter
  to a terminal, so big files report per-file rather than per-byte. Every other
  backend reports real progress.
- S3 and WebDAV uploads buffer the file in memory; fine to a few hundred MB,
  not a way to move a 20 GB disk image.
- One transfer at a time, deliberately.
- S3 signing is verified against an independent implementation in the tests,
  but has not been exercised against a live AWS endpoint.

## Tests

    python3 tests/run_all.py client

`test_ftp` and `test_dav_s3` start their own mock servers and need nothing
external. `test_sftp`, `test_queue` and `test_gui` transfer real files to whatever
`FOXZILLA_TEST_SSH_HOST` points at and clean up after themselves; `test_gui`
also needs a display.
