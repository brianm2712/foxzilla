"""FTP and FTPS, via stdlib ftplib."""
from __future__ import annotations

import datetime as dt
import functools
import os
import ssl
import threading
import time
from ftplib import FTP, FTP_TLS

from .base import CHUNK, Backend, Entry, _int, _ls_time


def _serial(method):
    """
    Run a method holding the backend's lock.

    FTP has one control connection, and two threads issuing commands on it at
    once (browsing a pane while the queue transfers, say) interleave their
    requests and replies. Every operation that talks to the server takes the
    lock, so they queue up instead.
    """
    @functools.wraps(method)
    def wrapper(self, *a, **k):
        with self._lock:
            return method(self, *a, **k)
    return wrapper


class FTPBackend(Backend):
    scheme = "ftp"
    max_parallel = 1        # one control connection, so strictly serial

    def __init__(self, host, user="anonymous", password="", port=21,
                 tls=False, passive=True, label=None, ca_file=None,
                 insecure_tls=False):
        self.host, self.port = host, port or 21
        self.user, self.password = user, password
        self.tls, self.passive = tls, passive
        # FTPS checks the server certificate like a browser does. A homelab
        # box with a self-signed cert either names its CA file here, or opts
        # out explicitly with insecure_tls - never silently.
        self.ca_file = ca_file or None
        self.insecure_tls = bool(insecure_tls)
        self._lock = threading.RLock()
        self.label = label or f"{'ftps' if tls else 'ftp'}://{user}@{host}"
        self.ftp = None

    def _tls_context(self):
        ctx = ssl.create_default_context(cafile=self.ca_file)
        if self.insecure_tls:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    @_serial
    def connect(self):
        self.ftp = FTP_TLS(context=self._tls_context()) if self.tls else FTP()
        self.ftp.connect(self.host, self.port, timeout=30)
        self.ftp.login(self.user, self.password)
        if self.tls:
            self.ftp.prot_p()          # encrypt the data channel too
        self.ftp.set_pasv(self.passive)
        try:
            self.ftp.encoding = "utf-8"
            self.ftp.sendcmd("OPTS UTF8 ON")
        except Exception:
            pass

    def close(self):
        try:
            self.ftp and self.ftp.quit()
        except Exception:
            try:
                self.ftp and self.ftp.close()
            except Exception:
                pass

    @_serial
    def home(self):
        return self.ftp.pwd() or "/"

    @_serial
    def listdir(self, path):
        entries = []
        # MLSD is the machine-readable listing; fall back to parsing LIST.
        try:
            for name, facts in self.ftp.mlsd(path,
                                             facts=["type", "size", "modify"]):
                t = facts.get("type", "")
                if t in ("cdir", "pdir") or name in (".", ".."):
                    continue
                entries.append(
                    Entry(
                        name=name,
                        size=_int(facts.get("size")),
                        mtime=_mlsd_time(facts.get("modify")),
                        is_dir=t == "dir",
                    )
                )
            return entries
        except Exception:
            pass
        lines = []
        self.ftp.retrlines(f"LIST {path}", lines.append)
        for line in lines:
            e = self._parse_list(line)
            if e and e.name not in (".", ".."):
                entries.append(e)
        return entries

    @staticmethod
    def _parse_list(line):
        parts = line.split(None, 8)
        if len(parts) < 9 or len(parts[0]) < 10:
            return None
        mode, _l, _o, _g, size, mon, day, timeyear, name = parts
        name = name.split(" -> ")[0]
        return Entry(
            name=name,
            size=0 if mode[0] == "d" else _int(size),
            mtime=_ls_time(mon, day, timeyear),
            is_dir=mode[0] == "d",
            is_link=mode[0] == "l",
            mode=mode,
        )

    @_serial
    def mkdir(self, path):
        self.ftp.mkd(path)

    @_serial
    def remove(self, path):
        self.ftp.delete(path)

    @_serial
    def rmdir(self, path):
        for e in self.listdir(path):
            child = self.join(path, e.name)
            self.rmdir(child) if e.is_dir else self.remove(child)
        self.ftp.rmd(path)

    @_serial
    def rename(self, src, dst):
        self.ftp.rename(src, dst)

    @_serial
    def read_to(self, path, local_path, progress=None):
        total = self.size(path)
        done = 0
        started = time.monotonic()
        with open(local_path, "wb") as fh:
            def got(buf):
                nonlocal done
                fh.write(buf)
                done += len(buf)
                if progress:
                    el = time.monotonic() - started
                    progress(done, total, done / el if el > 0.2 else 0)
            self._guarded(lambda: self.ftp.retrbinary(f"RETR {path}", got,
                                                      blocksize=CHUNK))

    @_serial
    def write_from(self, local_path, path, progress=None):
        total = os.path.getsize(local_path)
        done = 0
        started = time.monotonic()
        with open(local_path, "rb") as fh:
            def sending(buf):
                nonlocal done
                done += len(buf)
                if progress:
                    el = time.monotonic() - started
                    progress(done, total, done / el if el > 0.2 else 0)
            self._guarded(lambda: self.ftp.storbinary(f"STOR {path}", fh, blocksize=CHUNK,
                                                      callback=sending))

    def _guarded(self, transfer):
        """
        Run a data transfer; if it is interrupted, leave a clean connection.

        A transfer stopped part-way (cancel, or a local write error) leaves the
        server's reply to it unread on the control connection, so the next
        command would read that instead of its own answer. Send ABOR, then
        reconnect anyway: some servers answer ABOR oddly, and a fresh login
        is the one state that is certainly clean.
        """
        try:
            return transfer()
        except BaseException:
            try:
                self.ftp.abort()
            except Exception:                                # noqa: BLE001
                pass
            try:
                self.ftp.close()
            except Exception:                                # noqa: BLE001
                pass
            try:
                self.connect()
            except Exception:                                # noqa: BLE001
                pass                       # the next call will report it
            raise

    def size_of(self, path):
        return self.size(path)

    @_serial
    def size(self, path):
        try:
            self.ftp.voidcmd("TYPE I")
            return self.ftp.size(path) or 0
        except Exception:
            return 0


def _mlsd_time(s):
    try:
        return dt.datetime.strptime(s[:14], "%Y%m%d%H%M%S").timestamp()
    except (TypeError, ValueError):
        return 0.0
