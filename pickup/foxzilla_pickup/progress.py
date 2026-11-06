"""status.json progress reporting, and the --health check that reads it."""
from __future__ import annotations

import json
import os
import time

# cmd_whoami is looked up on the module (jellyfin.cmd_whoami) so tests can patch it there.
from . import jellyfin


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

    def __init__(self, path, clock=time.time, mono=time.monotonic, log=None):
        # clock: wall time, for the timestamps shown on the dashboard.
        # mono: monotonic, for pacing (throttle, rate) so a clock step can't stall it.
        self.path, self.clock, self.mono, self.log = path, clock, mono, log
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
        if not self.path:
            return
        active = (self.doc["active"] or {}).get("name")
        self.doc["queue"] = [dict(r) for r in rows if r.get("name") != active]

    def begin(self, name, phase, total, unit="bytes", files_total=None):
        if not self.path:
            return
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
        self._samples = [(self.mono(), 0)]
        self._write(force=True)

    def advance(self, n, file=None):
        if not self.path:
            return
        a = self.doc["active"]
        if a:
            self.set_done(a["done"] + n, file)

    def set_done(self, done, file=None):
        if not self.path:
            return
        a = self.doc["active"]
        if not a:
            return
        a["done"] = done
        if file is not None:
            a["file"] = file
        self._write()

    def file_done(self, file=None):
        if not self.path:
            return
        a = self.doc["active"]
        if not a:
            return
        a["files_done"] += 1
        if a["unit"] == "files":
            a["done"] = a["files_done"]
        if file is not None:
            a["file"] = file
        self._write()

    def touch(self):
        """Keep the card fresh during work that reports no bytes (throttled)."""
        if not self.path:
            return
        self._write()

    def finish(self, name, result, error=None):
        if not self.path:
            return
        self.doc["active"] = None
        self.doc["state"] = "idle"
        self.doc["last"] = {"name": name, "result": result,
                            "finished": self.clock(), "error": error}
        self._write(force=True)

    def staging(self, items):
        """Abandoned staging dirs, for a human to look at (never deleted)."""
        if not self.path:
            return
        self.doc["staging"] = [dict(r) for r in items]

    def idle(self):
        if not self.path:
            return
        self.doc["active"] = None
        self.doc["state"] = "idle"
        self._write(force=True)

    def _rate(self, now):
        # unit="files" phases (fast renames) intentionally get no rate/ETA.
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
        rate = max((d1 - d0) / (t1 - t0), 0.0)
        a["rate"] = round(rate, 3)
        total = a["total"]
        a["eta_s"] = (round((total - a["done"]) / rate, 1)
                      if isinstance(total, (int, float)) and rate > 0 else None)

    def _write(self, force=False):
        if not self.path:
            return
        now = self.mono()
        if not force and self._last_write is not None and now - self._last_write < self.THROTTLE:
            return
        self._last_write = now
        tmp = self.path + ".tmp"
        try:
            self._rate(now)
            self.doc["updated"] = self.clock()
            text = json.dumps(self.doc, default=str)
            with open(tmp, "w") as fh:
                fh.write(text)
            os.chmod(tmp, 0o644)
            os.replace(tmp, self.path)
            self.writes += 1
            self._failing = False
        except Exception as e:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            if not self._failing and self.log:
                try:
                    self.log(f"could not write status file {self.path}: {e}", "WARN")
                except Exception:
                    pass
            self._failing = True


NULL_PROGRESS = Progress("")


# --health: is the watcher alive? Idle runs come every 60s and always write
# "idle"; an active run rewrites status at least every few seconds while it
# progresses. A run that cannot take the lock writes nothing.
HEALTH_IDLE_MAX_AGE = 300      # seconds since an idle write
HEALTH_ACTIVE_MAX_AGE = 600    # seconds since an active write


def cmd_health(status_file, clock=time.time):
    """
    Exit 0 if status.json is fresh for its state, else 1 with one line of why.
    Read-only: takes no lock and writes nothing. No status file configured
    means there is nothing to judge by, so fall back to the credential check.
    """
    if not status_file:
        return jellyfin.cmd_whoami()
    try:
        with open(status_file) as fh:
            doc = json.load(fh)
        state, updated = doc.get("state"), float(doc["updated"])
    except FileNotFoundError:
        print(f"unhealthy: status file {status_file} missing")
        return 1
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as e:
        print(f"unhealthy: status file {status_file} unreadable ({type(e).__name__})")
        return 1
    age = clock() - updated
    if state == "idle":
        if age <= HEALTH_IDLE_MAX_AGE:
            return 0
        print(f"unhealthy: idle but status not updated for {age:.0f}s "
              f"(limit {HEALTH_IDLE_MAX_AGE}s)")
        return 1
    if state == "active":
        if age <= HEALTH_ACTIVE_MAX_AGE:
            return 0
        a = doc.get("active") or {}
        print(f"unhealthy: active on {a.get('name')!r} ({a.get('phase')}) but status "
              f"not updated for {age:.0f}s (limit {HEALTH_ACTIVE_MAX_AGE}s)")
        return 1
    print(f"unhealthy: unknown state {state!r} in {status_file}")
    return 1
