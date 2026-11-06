"""The transfer queue: ordered, reorderable, drained by a pool of workers."""
from __future__ import annotations

import os
import posixpath
import tempfile
import threading
import time
from dataclasses import dataclass, field

from .backends import Backend, LocalBackend, TransferError, safe_name, under_root
from .util import APP, VERSION


COMPLETE_SUFFIX = ".complete"
PART_SUFFIX = ".part"


def marker_path(directory: str, name: str) -> str:
    """Where the completion marker for `name` lives. Mirrors the watcher."""
    return posixpath.join(directory, f".{name}{COMPLETE_SUFFIX}")


def write_marker(backend, path):
    """Drop the '.<name>.complete' marker at `path` on `backend`."""
    fd, tmp = tempfile.mkstemp(prefix=f".{APP}-marker-")
    try:
        os.write(fd, f"verified by {APP} {VERSION}\n".encode())
        os.close(fd)
        backend.write_from(tmp, path)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


class Batch:
    """
    One item the user sent - a file or a folder - and every job it becomes.

    It decides when that item's completion marker may be written: once it is
    armed (all of the item's files are queued) and every one of its jobs has
    finished, with none failed or cancelled. `on_done(batch)` is called
    exactly once at that point, from whichever thread got there; `ok` and
    `reason` say whether a marker is earned.

    Owning its jobs is the point. The old rule - write every pending marker
    when the whole queue goes idle - fired early whenever the queue drained
    while a scan was still running, and ignored cancellations, and a marker
    makes the watcher skip its stability wait and file what it has.
    """

    TERMINAL_OK = ("done", "skipped")

    def __init__(self, name, on_done=None, dst=None, marker=None):
        self.name = name
        self.on_done = on_done
        self.dst, self.marker = dst, marker      # where its marker would go
        self.jobs: list[Job] = []
        self.armed = False
        self.fired = False
        self.ok = False
        self.reason = ""
        self._taint = ""
        self._lock = threading.Lock()

    def add(self, job: Job):
        with self._lock:
            job.batch = self
            self.jobs.append(job)

    def taint(self, reason: str):
        """Something outside the jobs means the item is not complete."""
        with self._lock:
            self._taint = self._taint or reason

    def arm(self):
        """All of the item's files are queued; it may now complete."""
        with self._lock:
            self.armed = True
        self.check()

    def check(self):
        with self._lock:
            if not self.armed or self.fired:
                return
            if any(j.state in ("queued", "running") for j in self.jobs):
                return
            self.fired = True
            failed = sum(1 for j in self.jobs if j.state == "failed")
            cancelled = sum(1 for j in self.jobs if j.state == "cancelled")
            if self._taint:
                self.reason = self._taint
            elif failed:
                self.reason = f"{failed} transfer(s) failed"
            elif cancelled:
                self.reason = f"{cancelled} transfer(s) cancelled"
            elif not self.jobs:
                self.reason = "nothing was transferred"
            self.ok = not self.reason
            callback = self.on_done
        if callback:
            callback(self)


@dataclass
class Job:
    src: Backend
    src_path: str
    dst: Backend
    dst_path: str
    is_dir: bool = False
    size: int = 0
    state: str = "queued"   # queued|running|done|failed|cancelled|skipped
    done: int = 0
    rate: float = 0.0
    error: str = ""
    row: str = ""           # treeview iid
    # The destination folder the user chose. Every path this job (and every
    # child it expands into) writes must stay inside it.
    dst_root: str = ""
    # The item this job belongs to, when the destination wants a completion
    # marker. Children of an expanded folder inherit it.
    batch: "Batch | None" = None
    cancel: threading.Event = field(default_factory=threading.Event)

    @property
    def name(self):
        return posixpath.basename(self.src_path.rstrip("/")) or self.src_path

    @property
    def active(self):
        return self.state in ("queued", "running")


class Queue:
    """
    An ordered, reorderable transfer queue drained by a pool of workers.

    Order *is* priority: the workers always take the topmost queued job they
    are allowed to run, so moving a row up the list genuinely promotes it.
    Per-backend limits are respected - FTP keeps a single control connection
    and must stay serial, while SFTP and the HTTP backends open independent
    ones and can run several at a time.
    """

    def __init__(self, on_change, workers=8, limit=3, skip_identical=False):
        self.jobs: list[Job] = []
        self.on_change = on_change
        self.skip_identical = skip_identical
        self.paused = False
        # Ceiling on concurrent transfers overall; per-backend limits still
        # apply on top, so FTP stays serial however high this goes.
        self.limit = limit
        self._lock = threading.Condition()
        self._inflight: dict[int, int] = {}      # id(backend) -> running count
        self._stop = False
        for _ in range(max(1, workers)):
            threading.Thread(target=self._worker, daemon=True).start()

    # -- queue management -------------------------------------------------

    def add(self, job: Job):
        with self._lock:
            self.jobs.append(job)
            self._lock.notify()
        self.on_change(job)

    def add_batch(self, batch: Batch | None, jobs: list[Job]):
        """Queue an item's jobs, then arm its batch - in that order."""
        for job in jobs:
            if batch is not None:
                batch.add(job)
            self.add(job)
        if batch is not None:
            batch.arm()

    @staticmethod
    def _settled(job: Job):
        if job.batch is not None:
            job.batch.check()

    def _insert_after(self, anchor: Job, children: list[Job]):
        with self._lock:
            try:
                at = self.jobs.index(anchor) + 1
            except ValueError:
                at = len(self.jobs)
            self.jobs[at:at] = children
            self._lock.notify_all()
        for c in children:
            # Joined before the parent finishes, so the batch can never see
            # "all done" in the gap between a folder and its contents.
            if anchor.batch is not None:
                anchor.batch.add(c)
            self.on_change(c)

    def move(self, job: Job, delta: int):
        """Shift a queued job up or down; -1 is one step towards the front."""
        with self._lock:
            if job not in self.jobs:
                return False
            i = self.jobs.index(job)
            j = max(0, min(len(self.jobs) - 1, i + delta))
            if i == j:
                return False
            self.jobs.insert(j, self.jobs.pop(i))
            self._lock.notify_all()
        return True

    def move_to(self, job: Job, index: int):
        with self._lock:
            if job not in self.jobs:
                return False
            self.jobs.remove(job)
            self.jobs.insert(max(0, min(len(self.jobs), index)), job)
            self._lock.notify_all()
        return True

    def cancel(self, job: Job):
        job.cancel.set()
        with self._lock:
            was_queued = job.state == "queued"
            if was_queued:
                job.state = "cancelled"
        if was_queued:
            self.on_change(job)
            self._settled(job)

    def cancel_all(self):
        with self._lock:
            targets = [j for j in self.jobs if j.active]
        for j in targets:
            self.cancel(j)

    def shutdown(self, timeout=5.0):
        """Cancel everything and give running transfers a moment to stop."""
        self.cancel_all()
        deadline = time.monotonic() + timeout
        while self.running and time.monotonic() < deadline:
            time.sleep(0.05)
        with self._lock:
            self._stop = True
            self._lock.notify_all()
        return not self.running

    FINISHED = ("done", "cancelled", "skipped")

    def remove_finished(self) -> list[Job]:
        """Drop finished jobs (failed ones stay, to be seen). Returns them."""
        with self._lock:
            gone = [j for j in self.jobs if j.state in self.FINISHED]
            self.jobs = [j for j in self.jobs if j.state not in self.FINISHED]
        return gone

    def set_limit(self, n: int):
        with self._lock:
            self.limit = max(1, int(n))
            self._lock.notify_all()

    def set_paused(self, paused: bool):
        with self._lock:
            self.paused = paused
            self._lock.notify_all()

    @property
    def running(self):
        return [j for j in self.jobs if j.state == "running"]

    @property
    def pending(self):
        return [j for j in self.jobs if j.state == "queued"]

    def idle(self):
        return not self.running and not self.pending

    # -- the pool ---------------------------------------------------------

    def _claim(self):
        """Take the highest-priority job whose backends have spare capacity."""
        with self._lock:
            while True:
                if self._stop:
                    return None
                busy = sum(1 for j in self.jobs if j.state == "running")
                if not self.paused and busy < self.limit:
                    for job in self.jobs:
                        if job.state != "queued":
                            continue
                        limit = min(job.src.max_parallel, job.dst.max_parallel)
                        used = max(self._inflight.get(id(job.src), 0),
                                   self._inflight.get(id(job.dst), 0))
                        if used < limit:
                            job.state = "running"
                            for b in (job.src, job.dst):
                                self._inflight[id(b)] = self._inflight.get(id(b), 0) + 1
                            return job
                self._lock.wait(0.5)

    def _release(self, job):
        with self._lock:
            for b in (job.src, job.dst):
                self._inflight[id(b)] = max(0, self._inflight.get(id(b), 1) - 1)
            self._lock.notify_all()

    def _worker(self):
        while True:
            job = self._claim()
            if job is None:
                return
            self.on_change(job)
            try:
                if job.cancel.is_set():
                    job.state = "cancelled"
                elif job.is_dir:
                    self._expand(job)
                    job.state = "done"
                elif self.skip_identical and self._already_there(job):
                    job.state = "skipped"
                else:
                    self._move_file(job)
                    job.state = "done"
            except Exception as e:                       # noqa: BLE001
                job.state = "cancelled" if job.cancel.is_set() else "failed"
                job.error = str(e)
            finally:
                self._release(job)
            self.on_change(job)
            self._settled(job)

    # -- the work itself --------------------------------------------------

    @staticmethod
    def _check_root(job: Job):
        """Refuse a destination outside the folder the user chose."""
        if job.dst_root and not under_root(job.dst_root, job.dst_path,
                                           local=job.dst.is_local):
            raise TransferError(f"refusing to write outside {job.dst_root}: "
                                f"{job.dst_path}")

    def _expand(self, job: Job):
        """Turn a directory into child jobs, queued right behind it."""
        self._check_root(job)
        try:
            job.dst.mkdir(job.dst_path)
        except Exception:                                # noqa: BLE001
            pass                                         # already there
        children = []
        for e in job.src.listdir(job.src_path):
            try:
                name = safe_name(e.name)
            except TransferError as bad:
                # Shown as a failed row, so it is seen and so it blocks any
                # completion marker; its siblings still transfer.
                children.append(Job(src=job.src, src_path=job.src_path + "/" + e.name,
                                    dst=job.dst, dst_path=job.dst_path, size=e.size,
                                    state="failed", error=str(bad),
                                    dst_root=job.dst_root))
                continue
            children.append(Job(src=job.src, src_path=job.src.join(job.src_path, name),
                                dst=job.dst, dst_path=posixpath.join(job.dst_path, name),
                                is_dir=e.is_dir, size=e.size, dst_root=job.dst_root))
        self._insert_after(job, children)

    def _already_there(self, job: Job) -> bool:
        """
        Is an identical file already at the destination?

        Sizes must match first - that alone rejects most candidates for free.
        Only then do we ask both sides for a hash, and only if both can
        produce one without transferring the file. When they cannot, a size
        match is all we have, so say so rather than pretend otherwise.
        """
        src_size = job.src.size_of(job.src_path)
        dst_size = job.dst.size_of(job.dst_path)
        if dst_size < 0 or src_size < 0 or src_size != dst_size:
            return False
        a = job.src.checksum(job.src_path)
        b = job.dst.checksum(job.dst_path) if a else None
        if a and b:
            return a == b
        job.error = "matched on size only (no remote hash available)"
        return True

    def _move_file(self, job: Job):
        src, dst = job.src, job.dst
        self._check_root(job)

        def progress(done, total, rate=0.0):
            if job.cancel.is_set():
                raise TransferError("cancelled")
            job.done, job.rate = done, rate
            if total:
                job.size = total
            self.on_change(job)

        if dst.is_local:
            # Into '<name>.part', renamed only once it is all there: a
            # cancelled, failed or killed download never leaves a truncated
            # file under the real name (or clobbers a good existing one).
            part = job.dst_path + PART_SUFFIX
            try:
                if src.is_local:
                    LocalBackend._copy(job.src_path, part, progress)
                else:
                    src.read_to(job.src_path, part, progress)
                os.replace(part, job.dst_path)
            except BaseException:
                try:
                    os.remove(part)
                except OSError:
                    pass
                raise
        elif src.is_local:
            dst.write_from(job.src_path, job.dst_path, progress)
        else:
            # remote to remote: relay through a temp file on this machine.
            tmp = os.path.join(tempfile.gettempdir(),
                               f".{APP}-relay-{os.getpid()}-{id(job):x}")
            try:
                src.read_to(job.src_path, tmp, progress)
                dst.write_from(tmp, job.dst_path, progress)
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
