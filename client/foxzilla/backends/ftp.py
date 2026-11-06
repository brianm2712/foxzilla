"""FTP and FTPS, via stdlib ftplib."""
from __future__ import annotations

import datetime as dt
import os
import ssl
import time
from ftplib import FTP, FTP_TLS

from .base import CHUNK, Backend, Entry, _int, _ls_time


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
        self.label = label or f"{'ftps' if tls else 'ftp'}://{user}@{host}"
        self.ftp = None

    def _tls_context(self):
        ctx = ssl.create_default_context(cafile=self.ca_file)
        if self.insecure_tls:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

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

    def home(self):
        return self.ftp.pwd() or "/"

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

    def mkdir(self, path):
        self.ftp.mkd(path)

    def remove(self, path):
        self.ftp.delete(path)

    def rmdir(self, path):
        for e in self.listdir(path):
            child = self.join(path, e.name)
            self.rmdir(child) if e.is_dir else self.remove(child)
        self.ftp.rmd(path)

    def rename(self, src, dst):
        self.ftp.rename(src, dst)

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
            self.ftp.retrbinary(f"RETR {path}", got, blocksize=CHUNK)

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
            self.ftp.storbinary(f"STOR {path}", fh, blocksize=CHUNK,
                                callback=sending)

    def size_of(self, path):
        return self.size(path)

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
