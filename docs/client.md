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

Copy `foxzilla.py` to any machine with Python 3.9+ and Tk and it runs —
Linux or macOS.

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

S3 credentials fall back to `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` when
left out of the site. FTP and WebDAV passwords are prompted for at connect and
are **not** stored unless you deliberately write them to
`~/.config/foxzilla/secrets.json`, which is plain JSON at 0600 — obfuscation, not
encryption.

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

    tests/run.sh

`test_ftp` and `test_dav_s3` start their own mock servers and need nothing
external. `test_sftp`, `test_queue` and `test_gui` transfer real files to whatever
`FOXZILLA_TEST_SSH_HOST` points at and clean up after themselves; `test_gui`
also needs a display.
