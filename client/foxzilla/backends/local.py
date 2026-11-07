"""The local disk."""
from __future__ import annotations

import hashlib
import os
import shutil
import stat as statmod

from .base import CHUNK, Backend, Entry, _pump


class LocalBackend(Backend):
    scheme = "file"
    is_local = True

    def __init__(self, label="Local", start=None):
        self.label = label
        self.start = start

    def home(self):
        # A local "site" can pin a starting directory, which is how removable
        # drives and scratch areas get to be one click away.
        if self.start and os.path.isdir(self.start):
            return self.start
        return os.path.expanduser("~")

    def start_missing(self):
        """The pinned start folder, if there is one and it isn't there -
        typically a removable drive that isn't mounted yet."""
        if self.start and not os.path.isdir(self.start):
            return self.start
        return None

    def listdir(self, path):
        out = []
        with os.scandir(path) as it:
            for de in it:
                try:
                    st = de.stat(follow_symlinks=True)
                    is_dir = de.is_dir(follow_symlinks=True)
                except OSError:
                    st = de.stat(follow_symlinks=False)
                    is_dir = False
                out.append(
                    Entry(
                        name=de.name,
                        size=0 if is_dir else st.st_size,
                        mtime=st.st_mtime,
                        is_dir=is_dir,
                        is_link=de.is_symlink(),
                        mode=statmod.filemode(st.st_mode),
                    )
                )
        return out

    def mkdir(self, path):
        os.mkdir(path)

    def remove(self, path):
        os.remove(path)

    def rmdir(self, path):
        shutil.rmtree(path)

    def rename(self, src, dst):
        os.rename(src, dst)

    def read_to(self, path, local_path, progress=None):
        self._copy(path, local_path, progress)

    def write_from(self, local_path, path, progress=None):
        self._copy(local_path, path, progress)

    def size_of(self, path):
        try:
            return os.path.getsize(path)
        except OSError:
            return -1

    def checksum(self, path):
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            while (buf := fh.read(CHUNK)):
                h.update(buf)
        return h.hexdigest()

    @staticmethod
    def _copy(a, b, progress):
        total = os.path.getsize(a)
        with open(a, "rb") as src, open(b, "wb") as dst:
            _pump(src, dst, total, progress)
