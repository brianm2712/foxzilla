"""The transfer queue: ordered, reorderable, drained by a pool of workers."""
from __future__ import annotations

import os
import posixpath
import tempfile
import threading
from dataclasses import dataclass, field

from .backends import Backend, LocalBackend, TransferError, safe_name, under_root
from .util import APP


COMPLETE_SUFFIX = ".complete"


def marker_path(directory: str, name: str) -> str:
    """Where the completion marker for `name` lives. Mirrors the watcher."""
    return posixpath.join(directory, f".{name}{COMPLETE_SUFFIX}")


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

    def _insert_after(self, anchor: Job, children: list[Job]):
        with self._lock:
            try:
                at = self.jobs.index(anchor) + 1
            except ValueError:
                at = len(self.jobs)
            self.jobs[at:at] = children
            self._lock.notify_all()
        for c in children:
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
        if job.state == "queued":
            job.state = "cancelled"
            self.on_change(job)

    def cancel_all(self):
        with self._lock:
            targets = [j for j in self.jobs if j.active]
        for j in targets:
            self.cancel(j)

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

        if src.is_local and dst.is_local:
            LocalBackend._copy(job.src_path, job.dst_path, progress)
        elif src.is_local:
            dst.write_from(job.src_path, job.dst_path, progress)
        elif dst.is_local:
            src.read_to(job.src_path, job.dst_path, progress)
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
