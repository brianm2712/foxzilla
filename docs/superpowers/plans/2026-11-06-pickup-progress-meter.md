# Pickup Progress Meter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** foxzilla-pickup writes a live `status.json` (the active item's phase, %, rate, ETA, plus the inbox queue), and a card on `dash.home.prosej.com` shows it.

**Architecture:** A `Progress` class in `media_pickup.py` owns a status document. It writes the document atomically with a throttle, and its write errors never escape. The existing phase functions (`build_manifest`, `stage`, `commit`, `remux_title`) take an optional progress argument that defaults to a no-op instance. `main` builds the queue rows and owns the lifecycle. The container writes into `/opt/revproxy/html/pickup/` through a bind mount, and nginx already serves that directory, so the dashboard card just polls a static file.

**Tech Stack:** Python 3.12 stdlib (no new deps), ffmpeg `-progress`, plain JS in the static dashboard page, Docker on the PVE host.

Spec: `docs/superpowers/specs/2026-11-06-pickup-progress-meter-design.md`

## Ground rules

- Tests are a plain script, `pickup/tests/test_pickup.py`, using `check(label, cond, extra)`. Run it with `cd ~/foxzilla/pickup && python3 tests/test_pickup.py`. The last line is `N passed, M failed`, and it exits 1 on failure. New tests go **just above** the final `print(f"\n{len(PASS)} passed ...")` block.
- Baseline: 101 passed, 0 failed. Every task must end with 0 failed.
- In the test `Env`, the inbox and the libraries share one filesystem, so `stage()` hardlinks and the phase is `verifying`. That is expected.
- Commit messages start with `pickup:` and end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Other sessions deploy to the PVE host too, so check the host's md5 before Task 8.

## File map

| File | Change |
|---|---|
| `pickup/media_pickup.py` | `Progress` class, `NULL_PROGRESS`, the `status_file` default; hooks in `sha256_file`, `build_manifest`, `stage`, `commit`, `remux_title`, `convert_disc_image`, `process_item`, `main` |
| `pickup/tests/test_pickup.py` | new tests (Tasks 1–6) |
| `pickup/docker-entrypoint.sh` | pass `--status-file` |
| `pickup/Dockerfile` | `ENV STATUS_FILE=` |
| `pickup/run.sh` | mount `/opt/revproxy/html/pickup` at `/status`, set `STATUS_FILE` |
| `~/revproxy-html/index.prosej.html` (copy of host `/opt/revproxy/html/index.html`) | new `#pickup` section + script |

---

### Task 1: `Progress` class (state, throttle, rate/ETA, atomic write)

**Files:**
- Modify: `pickup/media_pickup.py` (add after the `Log` class, about line 177; add `"status_file": ""` to `DEFAULTS`)
- Test: `pickup/tests/test_pickup.py`

- [ ] **Step 1: Write the failing tests** (insert above the final summary block)

```python
# ---------------------------------------------------------------- progress reporter
print("\nprogress: throttled, atomic, rate/eta from a rolling window")
e = Env()
sp = os.path.join(e.base, "status", "status.json"); os.makedirs(os.path.dirname(sp))
t = [100.0]
p = MP.Progress(sp, clock=lambda: t[0])
p.begin("Film", "copying", 1000, files_total=2)
check("begin writes immediately", p.writes == 1 and os.path.exists(sp), p.writes)
t[0] = 100.5; p.advance(100, file="a.mkv")
t[0] = 101.9; p.advance(100)
check("advance inside 2 s does not write", p.writes == 1, p.writes)
t[0] = 102.1; p.advance(0)
check("advance after 2 s writes", p.writes == 2, p.writes)
doc = json.load(open(sp))
check("status is active with done/total", doc["state"] == "active" and doc["active"]["done"] == 200
      and doc["active"]["total"] == 1000 and doc["active"]["file"] == "a.mkv", doc)
check("no rate before 5 s of samples", doc["active"]["rate"] is None and doc["active"]["eta_s"] is None, doc["active"])
t[0] = 106.0; p.set_done(600)
a = json.load(open(sp))["active"]
check("rate over the window", a["rate"] == 100.0 and a["eta_s"] == 4.0, a)
t[0] = 136.0; p.set_done(900)
a = json.load(open(sp))["active"]
check("old samples age out of the 30 s window", a["rate"] == 10.0 and a["eta_s"] == 10.0, a)
t[0] = 140.0; p.set_done(900)
a = json.load(open(sp))["active"]
check("a stalled transfer has no eta", a["eta_s"] is None, a)
t[0] = 140.1; p.file_done()
check("file_done counts", p.doc["active"]["files_done"] == 1)
p.finish("Film", "ok")
doc = json.load(open(sp))
check("finish always writes and clears active", p.writes == 6 and doc["active"] is None
      and doc["last"]["name"] == "Film" and doc["last"]["result"] == "ok", (p.writes, doc))
check("no tmp file left", os.listdir(os.path.dirname(sp)) == ["status.json"], os.listdir(os.path.dirname(sp)))
check("file is world-readable", oct(os.stat(sp).st_mode & 0o777) == "0o644", oct(os.stat(sp).st_mode))
p.queue([{"name": "Next", "bytes": 5, "files": 1, "state": "waiting"}]); p.idle()
doc = json.load(open(sp))
check("idle keeps last and the queue", doc["state"] == "idle" and doc["last"]["name"] == "Film"
      and doc["queue"][0]["name"] == "Next", doc)
p2 = MP.Progress(sp)
check("a new run inherits last from the file", p2.doc["last"]["name"] == "Film", p2.doc)
p2.queue([{"name": "Film2", "state": "waiting"}, {"name": "Other", "state": "waiting"}])
p2.begin("Film2", "hashing", 10)
check("begin drops the active item from the queue", [r["name"] for r in p2.doc["queue"]] == ["Other"], p2.doc["queue"])

print("\nprogress: an unwritable path warns once and never raises")
msgs = []
bad = MP.Progress("/nonexistent-dir/x/status.json", log=lambda m, level="INFO": msgs.append((level, m)))
bad.begin("A", "copying", 10); bad.finish("A", "ok"); bad.idle()
check("one warning, no exception", len(msgs) == 1 and msgs[0][0] == "WARN", msgs)

print("\nprogress: an empty path is a no-op")
nul = MP.Progress("")
nul.begin("A", "copying", 10); nul.advance(5); nul.finish("A", "ok"); nul.idle()
check("no writes", nul.writes == 0)
```

- [ ] **Step 2: Run the tests to check they fail**

Run: `cd ~/foxzilla/pickup && python3 tests/test_pickup.py 2>&1 | tail -5`
Expected: they crash with `AttributeError: module 'media_pickup' has no attribute 'Progress'`.

- [ ] **Step 3: Implement**

In `DEFAULTS`, add the line below after `"park_dir": "",`:

```python
    "status_file": "",     # live progress JSON for the dashboard; empty = off
```

After the `Log` class, add:

```python
# --------------------------------------------------------------------------
# progress reporting (the dashboard's pickup card reads this file)
# --------------------------------------------------------------------------

class Progress:
    """
    Owns status.json: the active item's phase and progress, the inbox queue,
    and the last finished item. Writes are atomic (tmp + os.replace) and
    throttled. A write failure is logged once and otherwise ignored: the meter
    must never be the reason a handoff fails.
    """
    THROTTLE = 2.0      # seconds between progress writes
    WINDOW = 30.0       # rolling window for the rate
    MIN_SPAN = 5.0      # no rate until the samples span this long

    def __init__(self, path, clock=time.time, log=None):
        self.path, self.clock, self.log = path, clock, log
        self.writes = 0
        self._last_write = None
        self._samples = []
        self._failing = False
        self.doc = {"version": 1, "updated": None, "state": "idle",
                    "active": None, "queue": [], "last": self._read_last()}

    def _read_last(self):
        if not self.path:
            return None
        try:
            with open(self.path) as fh:
                return json.load(fh).get("last")
        except (OSError, ValueError, AttributeError):
            return None

    def queue(self, rows):
        active = (self.doc["active"] or {}).get("name")
        self.doc["queue"] = [r for r in rows if r.get("name") != active]

    def begin(self, name, phase, total, unit="bytes", files_total=None):
        now = self.clock()
        prev = self.doc["active"] or {}
        self.doc["state"] = "active"
        self.doc["active"] = {
            "name": name, "phase": phase, "unit": unit,
            "done": 0, "total": total, "file": None,
            "files_done": 0, "files_total": files_total,
            "rate": None, "eta_s": None, "phase_started": now,
            "item_started": prev.get("item_started", now) if prev.get("name") == name else now,
        }
        self.doc["queue"] = [r for r in self.doc["queue"] if r.get("name") != name]
        self._samples = [(now, 0)]
        self._write(force=True)

    def advance(self, n, file=None):
        a = self.doc["active"]
        if a:
            self.set_done(a["done"] + n, file)

    def set_done(self, done, file=None):
        a = self.doc["active"]
        if not a:
            return
        a["done"] = done
        if file is not None:
            a["file"] = file
        self._write()

    def file_done(self, file=None):
        a = self.doc["active"]
        if not a:
            return
        a["files_done"] += 1
        if a["unit"] == "files":
            a["done"] = a["files_done"]
        if file is not None:
            a["file"] = file
        self._write()

    def finish(self, name, result, error=None):
        self.doc["active"] = None
        self.doc["state"] = "idle"
        self.doc["last"] = {"name": name, "result": result,
                            "finished": self.clock(), "error": error}
        self._write(force=True)

    def idle(self):
        self.doc["active"] = None
        self.doc["state"] = "idle"
        self._write(force=True)

    def _rate(self, now):
        a = self.doc["active"]
        if not a or a["unit"] == "files":
            return
        self._samples.append((now, a["done"]))
        while len(self._samples) > 2 and self._samples[0][0] < now - self.WINDOW:
            self._samples.pop(0)
        (t0, d0), (t1, d1) = self._samples[0], self._samples[-1]
        if t1 - t0 < self.MIN_SPAN:
            a["rate"], a["eta_s"] = None, None
            return
        rate = (d1 - d0) / (t1 - t0)
        a["rate"] = round(rate, 3)
        a["eta_s"] = round((a["total"] - a["done"]) / rate, 1) if rate > 0 else None

    def _write(self, force=False):
        if not self.path:
            return
        now = self.clock()
        if not force and self._last_write is not None and now - self._last_write < self.THROTTLE:
            return
        self._last_write = now
        self._rate(now)
        self.doc["updated"] = now
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w") as fh:
                json.dump(self.doc, fh)
            os.chmod(tmp, 0o644)
            os.replace(tmp, self.path)
            self.writes += 1
            self._failing = False
        except OSError as e:
            if not self._failing and self.log:
                self.log(f"could not write status file {self.path}: {e}", "WARN")
            self._failing = True


NULL_PROGRESS = Progress("")
```

Note: `begin` records the sample `(now, 0)` and then `_write(force=True)` calls `_rate`, which appends `(now, 0)` again. That duplicate is harmless (the span is 0, so the rate stays None).

The test's write count is 6: begin, then the advances at 102.1, 106.0, 136.0 and 140.0, then finish. `file_done` at 140.1 falls inside the throttle. If the count is off, recount against the throttle rule rather than editing the test to match.

- [ ] **Step 4: Run the tests**

Run: `cd ~/foxzilla/pickup && python3 tests/test_pickup.py 2>&1 | tail -3`
Expected: `1xx passed, 0 failed`.

- [ ] **Step 5: Commit**

```bash
cd ~/foxzilla && git add pickup/media_pickup.py pickup/tests/test_pickup.py && git commit -m "pickup: Progress reporter that writes a throttled, atomic status.json

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Report progress while hashing (phase 1)

**Files:**
- Modify: `pickup/media_pickup.py` (`sha256_file` at about line 204, `build_manifest` at about line 259)
- Test: `pickup/tests/test_pickup.py`

- [ ] **Step 1: Write the failing test**

```python
class Rec:
    """A stand-in Progress that remembers what it was told."""
    def __init__(self): self.phases = []; self.done = 0; self.files = 0; self.total = None; self.files_total = None
    def begin(self, name, phase, total, unit="bytes", files_total=None):
        self.phases.append((name, phase, unit)); self.total = total; self.files_total = files_total
        self.done = 0; self.files = 0
    def advance(self, n, file=None): self.done += n
    def set_done(self, d, file=None): self.done = d
    def file_done(self, file=None): self.files += 1
    def queue(self, rows): pass
    def finish(self, *a, **k): pass
    def idle(self): pass

print("\nprogress: hashing reports every byte and every file")
e = Env()
item = os.path.join(e.upload, "Pack")
mkfile(os.path.join(item, "a.mkv"), size=MP.CHUNK + 10); mkfile(os.path.join(item, "b.mkv"), size=300)
r = Rec()
m = MP.build_manifest(item, lambda *a, **k: None, progress=r)
check("hashing phase begun for the item", r.phases == [("Pack", "hashing", "bytes")], r.phases)
check("hashed bytes == total", r.done == r.total == m["total_bytes"], (r.done, r.total))
check("files counted", r.files == r.files_total == 2, (r.files, r.files_total))
```

- [ ] **Step 2: Run it to check it fails**

Run: `cd ~/foxzilla/pickup && python3 tests/test_pickup.py 2>&1 | tail -5`
Expected: `TypeError: build_manifest() got an unexpected keyword argument 'progress'`.

- [ ] **Step 3: Implement**

Replace `sha256_file` with:

```python
def sha256_file(path, hasher=None, on_chunk=None):
    h = hasher or hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            buf = fh.read(CHUNK)
            if not buf:
                break
            h.update(buf)
            if on_chunk:
                on_chunk(len(buf))
    return h.hexdigest()
```

Replace `build_manifest` with:

```python
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
            "sha256": sha256_file(full, on_chunk=lambda n, rel=rel: progress.advance(n, rel)),
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
```

`NULL_PROGRESS` is defined at module level in Task 1, before `build_manifest`, so the default argument resolves.

- [ ] **Step 4: Run the tests**

Run: `cd ~/foxzilla/pickup && python3 tests/test_pickup.py 2>&1 | tail -3`
Expected: 0 failed.

- [ ] **Step 5: Commit**

```bash
cd ~/foxzilla && git add pickup && git commit -m "pickup: report hashing progress while building the manifest

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Report progress while staging and committing (phases 2 and 3)

**Files:**
- Modify: `pickup/media_pickup.py` (`stage` at about line 341, `commit` at about line 511, `process_item` at about line 994)
- Test: `pickup/tests/test_pickup.py`

- [ ] **Step 1: Write the failing test**

```python
print("\nprogress: staging and commit run to 100%")
e = Env()
item = os.path.join(e.upload, "Pack")
mkfile(os.path.join(item, "a.mkv"), size=MP.CHUNK + 10); mkfile(os.path.join(item, "b.mkv"), size=300)
m = MP.build_manifest(item, lambda *a, **k: None)
r = Rec()
staging = os.path.join(e.shows, ".staging", "t1")
MP.stage(item, m, staging, lambda *a, **k: None, progress=r)
check("stage phase is verifying on one filesystem", r.phases[-1] == ("Pack", "verifying", "bytes"), r.phases)
check("stage reaches total bytes", r.done == r.total == m["total_bytes"], (r.done, r.total))
check("stage counts every file", r.files == r.files_total == 2, (r.files, r.files_total))
r2 = Rec()
r2.begin("Pack", "committing", 2, unit="files", files_total=2)
MP.commit(staging, os.path.join(e.shows, "Pack"), os.path.join(e.shows, ".quarantine"),
          lambda *a, **k: None, progress=r2)
check("commit counts every placed file", r2.files == 2, r2.files)

print("\nprogress: a whole process_item walks hashing -> stage -> committing")
e = Env()
mkfile(os.path.join(e.upload, "Show S01", "Show.S01E01.mkv"), size=500)
mkfile(os.path.join(e.upload, "Show S01", "Show.S01E02.mkv"), size=500)
r = Rec()
cfg = dict(MP.DEFAULTS, upload_dir=e.upload, movies_dest=e.movies, shows_dest=e.shows,
           receipts_dir=e.receipts)
MP.process_item(cfg, "Show S01", lambda *a, **k: None, progress=r)
check("phases in order", [p for _, p, _ in r.phases] == ["hashing", "verifying", "committing"], r.phases)
```

- [ ] **Step 2: Run it to check it fails**

Run: `cd ~/foxzilla/pickup && python3 tests/test_pickup.py 2>&1 | tail -5`
Expected: `TypeError: stage() got an unexpected keyword argument 'progress'`.

- [ ] **Step 3: Implement**

In `stage`, change the signature and add the `begin` call right after the `log(f"  phase 2: ...")` line:

```python
def stage(src_root, manifest, staging, log, dry_run=False, progress=NULL_PROGRESS):
```

```python
    if not dry_run:
        progress.begin(os.path.basename(src_root.rstrip("/")),
                       "verifying" if linking else "copying",
                       manifest["total_bytes"], files_total=len(manifest["files"]))
```

In the copy loop, after `h.update(buf)`, add:

```python
                    progress.advance(len(buf), entry["path"])
```

Replace `got = sha256_file(dst)` in the `else:` branch with:

```python
            got = sha256_file(dst, on_chunk=lambda n, p=entry["path"]: progress.advance(n, p))
```

Just before `placed.append(entry["path"])` at the end of the loop (not the dry-run one), add:

```python
        progress.file_done(entry["path"])
```

In `commit`, change the signature to `def commit(staging, dest, quarantine, log, dry_run=False, progress=NULL_PROGRESS):`.

- In the whole-directory branch, change the loop to:

```python
        for rel, _ in walk_files(dest):
            placements[rel] = os.path.join(dest, rel)
            progress.file_done(rel)
```

- In the merge loop, add `progress.file_done(rel)` as the first line of the `for rel, full in walk_files(staging):` body.

In `process_item`:
- Change the signature to `def process_item(cfg, name, log, dry_run=False, progress=NULL_PROGRESS):`.
- Change `build_manifest(src, log)` to `build_manifest(src, log, progress)`.
- Change `stage(src, manifest, staging, log, dry_run=dry_run)` to `stage(src, manifest, staging, log, dry_run=dry_run, progress=progress)`.
- Before `for target_title, staged_path in targets.items():`, add:

```python
        if not dry_run:
            progress.begin(name, "committing", len(manifest["files"]), unit="files",
                           files_total=len(manifest["files"]))
```

- Change the `commit(...)` call to pass `progress=progress`.

- [ ] **Step 4: Run the tests**

Run: `cd ~/foxzilla/pickup && python3 tests/test_pickup.py 2>&1 | tail -3`
Expected: 0 failed. All earlier tests still pass, because every new argument defaults to `NULL_PROGRESS`.

- [ ] **Step 5: Commit**

```bash
cd ~/foxzilla && git add pickup && git commit -m "pickup: report staging and commit progress

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Report remux progress from ffmpeg `-progress`

**Files:**
- Modify: `pickup/media_pickup.py` (`remux_title` at about line 857, `convert_disc_image` at about line 922)
- Test: `pickup/tests/test_pickup.py`

- [ ] **Step 1: Write the failing test**

```python
print("\nprogress: remux reads ffmpeg's -progress stream")
e = Env()
ffstubs(e.base, {1: 100.0})
enc = os.path.join(e.base, "bin", "ffmpeg")
with open(enc, "w") as fh:
    fh.write("#!/usr/bin/env python3\nimport sys\n"
             "assert '-progress' in sys.argv and sys.argv[sys.argv.index('-progress')+1] == 'pipe:1'\n"
             "print('out_time_us=1000000'); print('progress=continue')\n"
             "print('out_time_us=N/A')\n"
             "print('out_time_us=3500000'); print('progress=end')\n"
             "print('a warning', file=sys.stderr)\n"
             "open(sys.argv[-1],'wb').write(b'MKV'*10)\n")
os.chmod(enc, 0o755); MP.FFMPEG = enc
seen = []
out = os.path.join(e.base, "out.mkv")
MP.remux_title("x.iso", 1, out, on_time=seen.append)
check("out_time parsed in seconds, N/A skipped", seen == [1.0, 3.5], seen)
check("output still written", os.path.getsize(out) == 30)
MP.FFMPEG, MP.FFPROBE = "/nonexistent/ffmpeg", "/nonexistent/ffprobe"
```

- [ ] **Step 2: Run it to check it fails**

Run: `cd ~/foxzilla/pickup && python3 tests/test_pickup.py 2>&1 | tail -5`
Expected: `TypeError: remux_title() got an unexpected keyword argument 'on_time'`.

- [ ] **Step 3: Implement**

Replace `remux_title` with:

```python
def remux_title(iso, title_no, out_path, on_time=None):
    """Lossless: the title's video, every audio and subtitle stream, chapters."""
    cmd = [FFMPEG, "-nostdin", "-y", "-v", "error", "-progress", "pipe:1", "-nostats",
           "-f", "dvdvideo", "-title", str(title_no),
           "-i", iso, "-map", "0", "-c", "copy", "-f", "matroska", out_path]
    # stderr goes to a file, not a pipe: reading stdout to the end while
    # stderr fills its pipe buffer would deadlock ffmpeg.
    with tempfile.TemporaryFile(mode="w+") as err:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, text=True)
        for line in p.stdout:
            if on_time and line.startswith("out_time_us="):
                try:
                    on_time(int(line.split("=", 1)[1]) / 1e6)
                except ValueError:
                    pass
        rc = p.wait()
        err.seek(0)
        stderr = err.read()
    if rc != 0:
        tail = " | ".join(l for l in stderr.strip().splitlines()[-5:] if l.strip())
        raise PickupError(f"ffmpeg failed (rc {rc}): {tail or 'no output'}")
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise PickupError("ffmpeg produced no output")
```

In `convert_disc_image`, change the signature to `def convert_disc_image(cfg, name, log, progress=NULL_PROGRESS):`, and replace the `remux_title(...)` line inside its `try:` with:

```python
        progress.begin(name, "remuxing", best[1], unit="seconds")
        remux_title(iso, best[0], os.path.join(work, item + ".mkv"), on_time=progress.set_done)
```

- [ ] **Step 4: Run the tests**

Run: `cd ~/foxzilla/pickup && python3 tests/test_pickup.py 2>&1 | tail -3`
Expected: 0 failed. The existing "ffmpeg failure raises with stderr" test still passes, because the stub's stderr now reaches the error through the temp file.

- [ ] **Step 5: Commit**

```bash
cd ~/foxzilla && git add pickup && git commit -m "pickup: report remux progress from ffmpeg -progress

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Queue and lifecycle in `main`

**Files:**
- Modify: `pickup/media_pickup.py` (`main` at about line 1101)
- Test: `pickup/tests/test_pickup.py`

- [ ] **Step 1: Write the failing tests**

```python
print("\nprogress: a run writes the queue and ends idle with last")
e = Env()
sp = os.path.join(e.base, "status.json")
mkfile(os.path.join(e.upload, "Uploading", "a.mkv.part"), size=100)
mkfile(os.path.join(e.upload, "Film (2020).mkv"), size=2000)
e.run("--status-file", sp)
doc = json.load(open(sp))
check("run ends idle", doc["state"] == "idle" and doc["active"] is None, doc)
check("last is the filed film", doc["last"]["name"] == "Film (2020).mkv" and doc["last"]["result"] == "ok", doc["last"])
rows = {r["name"]: r for r in doc["queue"]}
check("uploading item queued as uploading", rows.get("Uploading", {}).get("state") == "uploading", doc["queue"])
check("queue row has size", rows["Uploading"]["bytes"] == 100 and rows["Uploading"]["files"] == 1, rows["Uploading"])
check("filed film is not in the queue", "Film (2020).mkv" not in rows, doc["queue"])

print("\nprogress: a failed item shows its error in the queue")
e = Env()
sp = os.path.join(e.base, "status.json")
os.makedirs(os.path.join(e.upload, "Empty"))
mkfile(os.path.join(e.upload, "Empty", ".DS_Store"), size=10)
e.run("--status-file", sp)
doc = json.load(open(sp))
rows = {r["name"]: r for r in doc["queue"]}
check("failed row with error", rows.get("Empty", {}).get("state") == "failed" and rows["Empty"].get("error"), doc["queue"])
check("last records the failure", doc["last"]["result"] == "failed", doc["last"])

print("\nprogress: a tick that cannot take the lock leaves the status alone")
e = Env()
sp = os.path.join(e.base, "status.json")
open(sp, "w").write('{"sentinel": true}')
held = MP.acquire_lock(e.lock)
mkfile(os.path.join(e.upload, "Film (2020).mkv"), size=2000)
e.run("--status-file", sp)
held.close()
check("status untouched", json.load(open(sp)) == {"sentinel": True})

print("\nprogress: an empty inbox still writes idle")
e = Env()
sp = os.path.join(e.base, "status.json")
e.run("--status-file", sp)
check("idle written with empty queue", json.load(open(sp))["state"] == "idle" and json.load(open(sp))["queue"] == [])
```

Note: if "Empty" containing only `.DS_Store` is filtered out before processing (check `is_junk` and `walk_files`), it fails with "item contains no usable files". That is the failure path this test wants. If instead the entry is skipped silently, replace it with an item that `process_item` rejects: monkeypatch `MP.process_item` to raise `MP.PickupError("boom")` for this one run, then restore it.

- [ ] **Step 2: Run them to check they fail**

Run: `cd ~/foxzilla/pickup && python3 tests/test_pickup.py 2>&1 | tail -8`
Expected: FAIL. The status file is never created, so the test errors with `FileNotFoundError`.

- [ ] **Step 3: Implement**

At module level, just above `def main`, add:

```python
def queue_row(name, now, state, error=None):
    row = {"name": name, "bytes": now["bytes"] if now else None,
           "files": now["files"] if now else None, "state": state}
    if error:
        row["error"] = error
    return row
```

In `main`, right after the lock check (`if lock is None: ... return 0`), add:

```python
    progress = Progress(cfg["status_file"], log=log)
    rows = {}
```

Inside the `try:`, right after `entries = [...]`, add:

```python
        rows.update((n, queue_row(n, None, "waiting")) for n in entries)
```

In the `for name in entries:` loop, after `prev = state.get(name, {})`, add:

```python
            err = prev.get("last_error")
```

Then set the row at each exit point. Put these immediately before the matching `continue`, or before the processing call:

| Branch | Line to add |
|---|---|
| disc image held (`state[name] = {... "held": "disc image"}`) | `rows[name] = queue_row(name, now, "held: disc image")` |
| unchanged failed convert (`state[name] = prev`) | `rows[name] = queue_row(name, now, "held: convert failed", err)` |
| partial files | `rows[name] = queue_row(name, now, "uploading", err)` |
| written to recently | `rows[name] = queue_row(name, now, "writing", err)` |
| not stable yet | `rows[name] = queue_row(name, now, f"stable {stable}/{STABLE_POLLS}", err)` |

Before `item = convert_disc_image(cfg, name, log)`, add `progress.queue(list(rows.values()))`, and change the call to `convert_disc_image(cfg, name, log, progress=progress)`. After `entries.append(item)`, add:

```python
                    rows.pop(name, None)
                    rows[item] = queue_row(item, None, "waiting")
                    progress.finish(name, "converted")
```

In both `except` branches of the convert, after `state[name] = {...}`, add:

```python
                    rows[name] = queue_row(name, now, "held: convert failed", state[name]["last_error"])
                    progress.finish(name, "failed", state[name]["last_error"])
```

Before `dests = process_item(...)`, add `progress.queue(list(rows.values()))`, and change the call to `process_item(cfg, name, log, dry_run=args.dry_run, progress=progress)`. After `state.pop(name, None)` in the success path, add:

```python
                rows.pop(name, None)
                if not args.dry_run:
                    progress.finish(name, "ok")
```

In both `except` branches of `process_item`, after `state[name] = {...}`, add:

```python
                rows[name] = queue_row(name, now, "failed", state[name]["last_error"])
                progress.finish(name, "failed", state[name]["last_error"])
```

Change the `finally:` to:

```python
    finally:
        progress.queue(list(rows.values()))
        progress.idle()
        lock.close()
```

`progress.idle()` writes even when the run raised. The function swallows `OSError` and raises nothing else, so it cannot mask the original exception.

- [ ] **Step 4: Run the tests**

Run: `cd ~/foxzilla/pickup && python3 tests/test_pickup.py 2>&1 | tail -3`
Expected: 0 failed.

- [ ] **Step 5: Commit**

```bash
cd ~/foxzilla && git add pickup && git commit -m "pickup: main writes the inbox queue and active/idle lifecycle to status.json

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Container wiring

**Files:**
- Modify: `pickup/docker-entrypoint.sh`, `pickup/Dockerfile`, `pickup/run.sh`

- [ ] **Step 1: Entrypoint.** In `docker-entrypoint.sh`, change the `args=` assignment's last line from `${PARK_DIR:+--park-dir $PARK_DIR}"` to:

```sh
${PARK_DIR:+--park-dir $PARK_DIR} ${STATUS_FILE:+--status-file $STATUS_FILE}"
```

- [ ] **Step 2: Dockerfile.** In the `ENV` block, after `PARK_DIR= \`, add:

```dockerfile
    STATUS_FILE= \
```

- [ ] **Step 3: run.sh.** Add `mkdir -p /opt/revproxy/html/pickup` before `docker rm -f`. Add `-e STATUS_FILE=/status/status.json` to the env lines, and `-v /opt/revproxy/html/pickup:/status` to the volume lines. Then add this comment under the existing header comment:

```sh
# /opt/revproxy/html/pickup is mounted at /status: the pickup writes status.json
# there and the dashboard (served from /opt/revproxy/html) polls it.
```

- [ ] **Step 4: Lint**

Run: `cd ~/foxzilla/pickup && sh -n docker-entrypoint.sh && sh -n run.sh && echo ok`
Expected: `ok`.

- [ ] **Step 5: Commit**

```bash
cd ~/foxzilla && git add pickup && git commit -m "pickup: container writes status.json into the dashboard's html/pickup

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Dashboard card

**Files:**
- Fetch: host `/opt/revproxy/html/index.html` → `~/revproxy-html/index.prosej.html` (the old Mac copy is missing)
- Modify: `~/revproxy-html/index.prosej.html`

- [ ] **Step 1: Fetch the live page**

```bash
mkdir -p ~/revproxy-html && scp -i ~/.ssh/proxmox root@100.102.227.73:/opt/revproxy/html/index.html ~/revproxy-html/index.prosej.html
```

This needs the sandbox off (the tailnet IP). `md5` the file and keep the value for Task 8.

- [ ] **Step 2: Add the section.** Insert it straight after the `</section>` that closes `<section id="media" ...>`:

```html
<section id="pickup" aria-label="Media pickup" style="margin-top: 20px; background: #12252c; border: 1px solid rgba(232,236,239,0.16); padding: 24px; display: flex; flex-direction: column; gap: 16px">
<div style="display: flex; flex-wrap: wrap; justify-content: space-between; gap: 12px; align-items: baseline">
<div style="font-family: 'JetBrains Mono', monospace; font-size: 11px; letter-spacing: 1.98px; color: #4fd8f5; text-transform: uppercase">07b · Media pickup → NAS</div>
</div>
<div style="display: flex; flex-wrap: wrap; align-items: center; gap: 14px">
<span id="pDot" style="width: 12px; height: 12px; border-radius: 999px; background: #8b989f"></span>
<span id="pHead" style="font-family: 'Space Grotesk', sans-serif; font-size: 24px; font-weight: 500">No data</span>
<span id="pWhen" style="font-family: 'JetBrains Mono', monospace; font-size: 12px; color: #8b989f">pickup/status.json not loaded</span>
<span id="pStale" hidden style="border: 1px solid #f0a030; color: #f0a030; padding: 2px 8px; font-family: 'JetBrains Mono', monospace; font-size: 10px; letter-spacing: 1.6px; text-transform: uppercase">stale — pickup not reporting</span>
</div>
<div id="pBarWrap" hidden role="progressbar" aria-valuemin="0" aria-valuemax="100" style="height: 10px; background: rgba(232,236,239,0.10)"><div id="pBar" style="height: 100%; width: 0; background: #4fd8f5; transition: width .6s"></div></div>
<div id="pMeta" style="font-family: 'JetBrains Mono', monospace; font-size: 12px; color: #8b989f"></div>
<ul id="pQueue" style="margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 4px; font-family: 'JetBrains Mono', monospace; font-size: 13px; color: #8b989f"></ul>
</section>
```

- [ ] **Step 3: Add the script** just before `</body>`:

```html
<!-- pickup section: painted from pickup/status.json, written by foxzilla-pickup (see ~/foxzilla/pickup) -->
<script>
(function(){
  var $=function(i){return document.getElementById(i);},OK='#5fd38a',WARN='#f0a030',GREY='#8b989f',ACC='#4fd8f5';
  var PH={hashing:'Hashing',copying:'Copying to NAS',verifying:'Verifying',committing:'Committing',remuxing:'Remuxing ISO'};
  function size(b){if(b==null)return '';var u=['B','KB','MB','GB','TB'],i=0;while(b>=1024&&i<4){b/=1024;i++;}return b.toFixed(1)+' '+u[i];}
  function dur(s){s=Math.max(0,Math.round(s));var h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;return h?h+'h '+m+'m':m?m+'m '+x+'s':x+'s';}
  function when(t){return new Date(t*1000).toLocaleString([],{weekday:'short',hour:'2-digit',minute:'2-digit'});}
  function dot(c){$('pDot').style.background=c;$('pDot').style.boxShadow=c===GREY?'none':'0 0 8px '+c;}
  function render(s){
    var q=(s&&s.queue)||[],a=s&&s.active;
    $('pQueue').innerHTML='';
    if(!s){dot(GREY);$('pHead').textContent='Pickup status unavailable';$('pWhen').textContent='pickup/status.json missing or unreadable';$('pWhen').style.color=GREY;$('pBarWrap').hidden=true;$('pMeta').textContent='';$('pStale').hidden=true;return 30000;}
    var stale=s.state==='active'&&(Date.now()/1000-s.updated)>180;
    $('pStale').hidden=!stale;
    if(s.state==='active'&&a){
      var pct=a.total?Math.min(100,100*a.done/a.total):0;
      dot(stale?WARN:ACC);
      $('pHead').textContent=a.name;
      $('pWhen').textContent=(PH[a.phase]||a.phase)+' · '+pct.toFixed(1)+'%';$('pWhen').style.color=GREY;
      $('pBarWrap').hidden=false;$('pBarWrap').setAttribute('aria-valuenow',pct.toFixed(0));
      $('pBar').style.width=pct+'%';$('pBar').style.background=stale?GREY:ACC;
      var m=[];
      if(a.unit==='bytes'){m.push(size(a.done)+' / '+size(a.total));if(a.rate)m.push(size(a.rate)+'/s');}
      else if(a.unit==='seconds'){m.push(dur(a.done)+' / '+dur(a.total)+' of film');if(a.rate)m.push(a.rate.toFixed(1)+'× realtime');}
      if(a.eta_s!=null&&!stale)m.push('ETA '+dur(a.eta_s));
      if(a.files_total)m.push(a.files_done+'/'+a.files_total+' files');
      m.push('item started '+when(a.item_started));
      $('pMeta').textContent=m.join(' · ');$('pMeta').title=a.file||'';
    }else{
      var l=s.last,bad=l&&l.result==='failed';
      dot(bad?WARN:OK);
      $('pHead').textContent='Idle';
      $('pWhen').textContent=l?('last: '+l.name+' · '+(l.result==='ok'?'filed':l.result)+' '+when(l.finished)):'nothing processed yet';
      $('pWhen').style.color=bad?WARN:GREY;$('pWhen').title=(l&&l.error)||'';
      $('pBarWrap').hidden=true;$('pMeta').textContent='';
    }
    q.forEach(function(r){
      var li=document.createElement('li'),bad=/held|failed/.test(r.state)||!!r.error;
      li.textContent=r.name+(r.bytes!=null?' · '+size(r.bytes):'')+' · '+r.state;
      li.style.color=bad?WARN:GREY;if(r.error)li.title=r.error;
      $('pQueue').appendChild(li);
    });
    return s.state==='active'?5000:30000;
  }
  function load(){
    fetch('pickup/status.json',{cache:'no-store'}).then(function(r){return r.ok?r.json():null;}).catch(function(){return null;})
      .then(function(s){var next=30000;try{next=render(s);}catch(e){console.error('pickup render',e);render(null);}setTimeout(load,next);});
  }
  load();
})();
</script>
```

- [ ] **Step 4: Check it in a browser against canned files.** Make a scratch dir with this page plus `pickup/status.json`, and serve it through `preview_start` (add a `pickup-card` entry to `~/foxzilla/.claude/launch.json` that runs `python3 -m http.server 8765 --directory <scratch>`). Swap the JSON between four cases, waiting about 30 s or reloading each time:
  1. `active`, copying, `updated` = now. The bar shows the %, rate, ETA and files count, and the queue rows show, with a failed row in orange and its tooltip.
  2. `active` with `updated` = now − 600. The stale badge shows, the bar is grey and there's no ETA.
  3. `idle` with `last` ok. It reads "Idle · last: X · filed <time>".
  4. The file is deleted. It reads "Pickup status unavailable", and the rest of the page is unaffected.

  Take a screenshot of each case. Then stop the preview.

- [ ] **Step 5: Commit the page copy** (`~/revproxy-html` is not a git repo, so record the change in the foxzilla repo instead):

```bash
mkdir -p ~/foxzilla/dashboard && cp ~/revproxy-html/index.prosej.html ~/foxzilla/dashboard/pickup-card.reference.html
cd ~/foxzilla && git add dashboard && git commit -m "dashboard: reference copy of the page with the pickup card

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Deploy (PVE host)

All commands run via `ssh -i ~/.ssh/proxmox root@100.102.227.73` with the sandbox off.

- [ ] **Step 1: Pre-flight.** The host's `md5sum /root/foxzilla-build/media_pickup.py` must equal `f3fe905849b39833c8a1ac68fc2d81b3` (the pre-change version). If it doesn't, stop: another session deployed, so merge first. Also check the dashboard's md5 still matches the value saved in Task 7, Step 1.
- [ ] **Step 2: Ship and test on the host.**

```bash
scp -i ~/.ssh/proxmox ~/foxzilla/pickup/{media_pickup.py,docker-entrypoint.sh,Dockerfile,run.sh} root@100.102.227.73:/root/foxzilla-build/
scp -i ~/.ssh/proxmox ~/foxzilla/pickup/tests/test_pickup.py root@100.102.227.73:/root/foxzilla-build/tests/
ssh -i ~/.ssh/proxmox root@100.102.227.73 'cd /root/foxzilla-build && python3 tests/test_pickup.py | tail -2 && docker build -q -t foxzilla-pickup:latest .'
```

  Expected: `N passed, 0 failed` and an image id.
- [ ] **Step 3: Wait until the pickup is idle.** `run.sh` kills an in-flight handoff, so the source survives but the copy restarts. Check `tail -3 /srv/foxzilla-config/pickup.log`: the last line must not be a phase line for an item that is still running, and `ls /mnt/nas-media/{movies,tv}/.staging` must be empty. If a job is running, wait. Do not interrupt it.
- [ ] **Step 4: Recreate the container.** Run `cd /root/foxzilla-build && ./run.sh`, then `docker ps --filter name=foxzilla-pickup`. Remember the AppArmor quirk ([[proxmox-docker-apparmor-signals]]): `docker rm -f` should still work, but if the old container survives, check with `docker ps -a`.
- [ ] **Step 5: Check the file appears.** Within about 70 s, `cat /opt/revproxy/html/pickup/status.json` shows `"state": "idle"`, and `curl -sk https://dash.home.prosej.com/pickup/status.json` returns the same JSON.
- [ ] **Step 6: Install the dashboard.**

```bash
ssh -i ~/.ssh/proxmox root@100.102.227.73 'cp /opt/revproxy/html/index.html /opt/revproxy/html/index.html.bak-$(date +%Y%m%d)-pickup'
scp -i ~/.ssh/proxmox ~/revproxy-html/index.prosej.html root@100.102.227.73:/opt/revproxy/html/index.html
```

  This is a static file in a bind-mounted directory, so there's no nginx reload and no revproxy change.
- [ ] **Step 7: Live check.** Open `https://dash.home.prosej.com` in Chrome. The pickup card shows Idle with the right queue. When the next real item is processed, the bar moves.
- [ ] **Step 8: Update memory** `project_foxzilla_pickup.md`: status.json path, the card, and the new run.sh mount.

---

## Self-review notes

- Spec coverage:
  - reporter: Task 1
  - phases: Tasks 2–4
  - queue and lifecycle: Task 5
  - "lock-skipped tick doesn't touch the file": Task 5 test
  - wiring: Task 6
  - card, stale and missing states: Task 7
  - deploy only when idle: Task 8
- Known limit: the queue is a snapshot taken when the run starts (plus convert outputs). Items dropped into the inbox during a long run appear on the next run. This matches the existing one-run-at-a-time lock.
- Placement change from the spec: dashboard sections are full-width blocks, not a row. The card goes directly after "07 · Media library" as "07b".
