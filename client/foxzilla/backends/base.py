"""The Backend interface, the listing Entry, and helpers the backends share."""
from __future__ import annotations

import datetime as dt
import posixpath
import re
import time
from dataclasses import dataclass

CHUNK = 256 * 1024


@dataclass
class Entry:
    """One row in a directory listing, normalised across every backend."""
    name: str
    size: int = 0
    mtime: float = 0.0
    is_dir: bool = False
    is_link: bool = False
    mode: str = ""

    @property
    def sort_key(self):
        return (not self.is_dir, self.name.lower())


class TransferError(Exception):
    pass


class Backend:
    """A place files live. Paths are always posix-style and absolute."""

    scheme = "?"
    label = "?"
    # True when the backend has a real local path for a file, letting us
    # hand os-level work (and fast copies) straight to the filesystem.
    is_local = False
    # Concurrent transfers this backend tolerates. FTP keeps one control
    # connection and must stay at 1; the rest spawn independent connections.
    max_parallel = 4
    # Set on sites that feed a watcher: once a transfer here is complete and
    # verified we drop a marker beside it, so the far side can act at once
    # instead of waiting out its own "has this stopped changing yet" guess.
    signal_complete = False

    def connect(self):
        pass

    def close(self):
        pass

    def home(self) -> str:
        return "/"

    def listdir(self, path: str) -> list[Entry]:
        raise NotImplementedError

    def mkdir(self, path: str):
        raise NotImplementedError

    def remove(self, path: str):
        raise NotImplementedError

    def rmdir(self, path: str):
        raise NotImplementedError

    def rename(self, src: str, dst: str):
        raise NotImplementedError

    def read_to(self, path: str, local_path: str, progress=None):
        """Copy remote `path` down to a real local file."""
        raise NotImplementedError

    def write_from(self, local_path: str, path: str, progress=None):
        """Copy a real local file up to remote `path`."""
        raise NotImplementedError

    def size_of(self, path: str) -> int:
        """Size in bytes, or -1 when the path is absent or unknowable."""
        return -1

    def checksum(self, path: str) -> str | None:
        """
        SHA-256 of a remote file, or None when the backend cannot produce one
        without downloading it. Used to skip files already at the destination.
        """
        return None

    # -- path helpers; posix everywhere, including on Windows remotes ------

    @staticmethod
    def join(base: str, name: str) -> str:
        return posixpath.normpath(posixpath.join(base, name))

    @staticmethod
    def parent(path: str) -> str:
        p = posixpath.dirname(path.rstrip("/"))
        return p or "/"

    def __str__(self):
        return self.label


def _pump(src, dst, total, progress, done=0):
    """Shovel bytes from a reader to a writer, reporting progress and rate."""
    started = time.monotonic()
    base = done
    while True:
        buf = src.read(CHUNK)
        if not buf:
            break
        dst.write(buf)
        done += len(buf)
        if progress:
            elapsed = time.monotonic() - started
            progress(done, total, (done - base) / elapsed if elapsed > 0.2 else 0)
    return done


def _int(s, default=0):
    try:
        return int(s)
    except (TypeError, ValueError):
        return default


def _ls_time(mon, day, timeyear):
    """Best-effort mtime from an `ls -l` stamp ('Aug 31 17:54' or 'Aug 31 2025')."""
    months = ("jan feb mar apr may jun jul aug sep oct nov dec").split()
    try:
        m = months.index(mon.lower()[:3]) + 1
        d = int(day)
    except ValueError:
        return 0.0
    now = dt.datetime.now()
    try:
        if ":" in timeyear:
            hh, mm = (int(x) for x in timeyear.split(":"))
            when = dt.datetime(now.year, m, d, hh, mm)
            # ls omits the year for recent files; if that lands in the
            # future it must have been last year.
            if when > now + dt.timedelta(days=1):
                when = when.replace(year=now.year - 1)
        else:
            when = dt.datetime(int(timeyear), m, d)
    except ValueError:
        return 0.0
    return when.timestamp()


def clean_typed_path(text):
    """
    A path as typed or pasted into the path bar, minus the shell quoting that
    would otherwise make it "not found": surrounding whitespace, one pair of
    matching quotes, and backslash escapes like `SEAGATE\\ EXP`.

    Only a backslash before punctuation or a space is an escape, so a
    Windows-style `C:\\Users` on an FTP server comes through untouched.
    """
    p = text.strip()
    if len(p) >= 2 and p[0] == p[-1] and p[0] in "'\"":
        return p[1:-1]
    return re.sub(r"\\([^A-Za-z0-9])", r"\1", p)
