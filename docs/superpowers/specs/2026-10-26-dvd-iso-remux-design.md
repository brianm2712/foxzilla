# DVD ISO → MKV remux inside the pickup

Date: 2026-10-26. Agreed with Brian in conversation (remux, not re-encode;
inside the pickup, not a sidecar; year from Jellyfin's metadata search with a
`.name` override file).

## Problem

Uploads of DVD-Video disc images (`.iso` / `.img`) land in `/mnt/incoming/upload`.
Jellyfin cannot play disc images, so since 2026-10-26 the pickup holds them in
the inbox with one WARN. Three are waiting: `MY_LEFT_FOOT.iso`, `BBCDVD3090.iso`
(The Snapper), `LES_MISERABLES.iso` (2012 musical). They need to become MKVs
that the existing verified handoff files into Movies.

## Facts established on the host

- Debian trixie's `ffmpeg` package (7.1) is built with libdvdread/libdvdnav and
  has the `dvdvideo` demuxer: `ffmpeg -f dvdvideo -title N -i disc.iso` reads
  the ISO file directly. No loop mount, no privileges, no `isoinfo`.
- `ffprobe -f dvdvideo -title N` is fast (reads the IFOs) and reports duration,
  streams and language tags per title. A title index past the end fails with
  "Stream not found"; an empty title in the middle (Les Misérables title 3)
  fails with "Invalid data", and later titles are fine.
- Main titles found: My Left Foot title 1 (98.8 min, 24 chapters, 1×AC3);
  BBCDVD3090 title 2 (91 min, eng AC3 + eng subs); Les Misérables title 11
  (151 min, 2×AC3 eng, subs eng + ice).
- jellyfin-ffmpeg inside the `jellyfin` container lacks the demuxer, and the
  pickup image (`python:3.12-slim`) is already trixie-based.
- The pickup's stored Jellyfin token works for `POST /Items/RemoteSearch/Movie`;
  the first TheMovieDb result is the right film for all three titles.

## Design

### Flow (change to `main()` in `media_pickup.py`)

The `holding … disc image` branch is replaced. An item whose top-level files
include a disc image is treated like any upload for the stability wait (partial
files, quiet period, two stable polls, or a `.complete` marker), then
`convert_disc_image()` runs instead of `process_item()`.

`convert_disc_image(cfg, name, log)`:

1. **Label**: read the ISO9660 primary volume descriptor at sector 16; the
   volume id (32 bytes at offset 40) becomes the label. Unreadable → PickupError.
2. **Name**: if `<iso>.name` exists beside the ISO its first non-empty line is
   the item name verbatim (e.g. `The Snapper (1993)`). Otherwise the label is
   title-cased (`MY_LEFT_FOOT` → `My Left Foot`; `_`/`.` → space; tokens that
   are all letters are capitalised, others kept) and Jellyfin's metadata search
   is asked for the year: first result from `TheMovieDb` whose name, lowercased
   and with accents stripped, starts with the query → use that result's name
   (keeps the accent: `Les Misérables`) and year → `Les Misérables (2012)`.
   No match, no credential or any network error → label-only name, logged once.
3. **Title**: `ffprobe -f dvdvideo -title N -i iso` for N = 1, 2, … collecting
   durations; stop after 3 consecutive failures; pick the longest. None →
   PickupError.
4. **Remux**: `ffmpeg -nostdin -y -f dvdvideo -title N -i iso -map 0 -c copy
   -f matroska <upload>/.converting/<Name>/<Name>.mkv`. All audio and subtitle
   streams and chapters are kept. Non-zero exit → PickupError carrying the
   last lines of stderr; the `.converting` dir is removed.
5. **Hand over**: rename `.converting/<Name>` to `<upload>/<Name>` (same
   filesystem, atomic), write the `.complete` marker for it, then rename the
   ISO and its `.name` file into `<park_dir>/.removed-YYYYMMDD/`. The ISO is
   never deleted. If `<upload>/<Name>` already exists the step fails before
   ffmpeg runs ("converted item already in the inbox").

The next tick files `<Name>/<Name>.mkv` through the normal hashed three-phase
handoff (manifest, stage, commit, reconcile), removes the folder and rescans
Jellyfin. The pickup log for a disc therefore reads: watching → converting →
converted → processing → committed, with no `holding` line.

### Failure handling

- ffmpeg or ffprobe missing (tests, an old image): the existing hold branch
  stays, with its WARN logged once.
- Conversion error: WARN once with the error, `state[name] = {"shape": …,
  "held": "convert failed", "last_error": …}`. Not retried while the ISO's
  shape is unchanged and no `.name` file has appeared, so a bad disc does not
  loop every tick. Any change to the item (re-upload, `.name` dropped beside
  it, marker) clears the hold.
- Nothing is written to the libraries by this step; the handoff's invariants
  are untouched.

### Configuration

- `park_dir` (`--park-dir`, env `PARK_DIR`): where `.removed-YYYYMMDD/` is
  created. Defaults to the upload dir (a dot-dir there is ignored by the
  pickup). `run.sh` mounts `/mnt/incoming` at `/incoming` and sets
  `UPLOAD_DIR=/incoming/upload`, `PARK_DIR=/incoming`, so parked ISOs sit
  beside the existing `/mnt/incoming/.hold`.
- `FFMPEG` / `FFPROBE` env (default `ffmpeg` / `ffprobe` on PATH) so tests
  can point at a stub or at nothing.
- Dockerfile: `apt-get install ffmpeg` (trixie). Image grows ~400 MB.

### Tests (`tests/test_pickup.py`, existing style)

A stub `ffprobe`/`ffmpeg` pair written into a temp dir: ffprobe answers
`-title N` from a per-test table (duration or failure), ffmpeg writes a file of
a few KB at the output path, or fails when told to. `jf_movie_lookup` is
monkeypatched. Synthetic ISOs are 17 sectors with `CD001` and a label at
sector 16.

- stable ISO converts end to end: item `My Left Foot (1989)/My Left Foot (1989).mkv`
  appears, ISO parked under `.removed-<today>`, not deleted; the following run
  files it in Movies; no `holding` line in the log
- `.name` file wins over label and lookup, and is parked with the ISO
- lookup failure → `My Left Foot` (label only) and a WARN
- longest title wins, with a failing title in the middle
- ffmpeg failure: ISO stays, one WARN, second run does not re-run ffmpeg
- label title-casing cases
- the existing hold test, run with `FFMPEG` pointed at a missing binary

### Deploy and proof

Same as the pickup: scp to `/root/foxzilla-build`, tests there, `docker build`,
`./run.sh`. Drop `BBCDVD3090.iso.name` containing `The Snapper (1993)` beside
the ISO. Done means the three MKVs are in `/mnt/nas-media/movies`, Jellyfin
lists them with direct-play media sources, the three ISOs are in
`/mnt/incoming/.removed-20261026/`, and the pickup log shows no `holding` lines
for them.

### Out of scope

NVENC re-encoding (lossless remux agreed), Blu-ray images, multi-episode TV
discs (a disc is always filed as one movie), the `dvd-ripper-dev` container
(a makemkvcon GUI for physical discs; unrelated).
