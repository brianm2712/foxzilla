#!/usr/bin/env python3
"""
Foxzilla - a small, dependency-free file transfer client.

A trimmed-down FileZilla: two panes, a queue, and nothing else.
Speaks SFTP, FTP/FTPS, S3 and WebDAV, plus the local disk.

Runs on any machine with Python 3.9+ and Tk. No pip install, ever:
  - SFTP  drives the OpenSSH `sftp` binary, so it inherits your keys,
          your ssh-agent and your ~/.ssh/config host aliases for free.
  - FTP   stdlib ftplib.
  - S3    hand-rolled SigV4 over urllib.
  - DAV   stdlib http.client + ElementTree.

    python3 foxzilla.py
"""

from __future__ import annotations

import base64
import datetime as dt
import errno
import hashlib
import hmac
import io
import json
import os
import posixpath
import queue
import re
import shutil
import stat as statmod
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from ftplib import FTP, FTP_TLS, error_perm

APP = "foxzilla"
VERSION = "0.2"

# Foxers palette - charcoal and concrete with an amber accent, taken from
# foxers-demo.html so the tool matches the rest of the kit.
THEME = {
    "bg":       "#1C1E21",   # charcoal, the window ground
    "panel":    "#2E3238",   # graphite, raised surfaces
    "field":    "#24272B",   # input and list backgrounds
    "text":     "#F4F4F2",   # off-white
    "muted":    "#8A9099",
    "line":     "#3A3F45",
    "accent":   "#FFB020",   # amber
    "accent_d": "#9C6500",   # pressed / borders
    "ok":       "#2E7D4F",
    "err":      "#B3261E",
    "sel":      "#3A3226",   # amber-tinted selection
}


def apply_theme(root):
    """
    Paint the whole window in the Foxers palette.

    Tk's default widgets ignore most styling, so this leans on ttk's 'clam'
    theme (the one theme that honours every colour we set) and configures the
    classic tk widgets - menus, the log Text - by hand.
    """
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    t = THEME
    root.configure(bg=t["bg"])
    style.configure(".", background=t["bg"], foreground=t["text"],
                    fieldbackground=t["field"], bordercolor=t["line"],
                    lightcolor=t["panel"], darkcolor=t["bg"],
                    focuscolor=t["accent"], insertcolor=t["text"])
    style.configure("TFrame", background=t["bg"])
    style.configure("TLabel", background=t["bg"], foreground=t["text"])
    style.configure("Hint.TLabel", background=t["bg"], foreground=t["muted"])
    style.configure("TCheckbutton", background=t["bg"], foreground=t["text"])
    style.map("TCheckbutton",
              background=[("active", t["bg"])],
              indicatorcolor=[("selected", t["accent"])])
    style.configure("TButton", background=t["panel"], foreground=t["text"],
                    bordercolor=t["line"], focusthickness=1, padding=(8, 3))
    style.map("TButton",
              background=[("active", t["line"]), ("pressed", t["accent_d"])],
              foreground=[("disabled", t["muted"])])
    style.configure("Accent.TButton", background=t["accent"],
                    foreground=t["bg"], bordercolor=t["accent_d"])
    style.map("Accent.TButton",
              background=[("active", "#FFC14D"), ("pressed", t["accent_d"])],
              foreground=[("active", t["bg"])])
    style.configure("TEntry", fieldbackground=t["field"], foreground=t["text"],
                    bordercolor=t["line"], insertcolor=t["text"])
    style.configure("TCombobox", fieldbackground=t["field"], background=t["panel"],
                    foreground=t["text"], arrowcolor=t["accent"])
    style.map("TCombobox", fieldbackground=[("readonly", t["field"])],
              foreground=[("readonly", t["text"])])
    root.option_add("*TCombobox*Listbox.background", t["field"])
    root.option_add("*TCombobox*Listbox.foreground", t["text"])
    root.option_add("*TCombobox*Listbox.selectBackground", t["accent"])
    root.option_add("*TCombobox*Listbox.selectForeground", t["bg"])
    style.configure("Treeview", background=t["field"], fieldbackground=t["field"],
                    foreground=t["text"], bordercolor=t["line"], rowheight=22)
    style.configure("Treeview.Heading", background=t["panel"],
                    foreground=t["muted"], relief="flat", padding=(6, 4))
    style.map("Treeview.Heading", background=[("active", t["line"])],
              foreground=[("active", t["accent"])])
    style.map("Treeview", background=[("selected", t["sel"])],
              foreground=[("selected", t["accent"])])
    style.configure("TNotebook", background=t["bg"], bordercolor=t["line"])
    style.configure("TNotebook.Tab", background=t["panel"], foreground=t["muted"],
                    padding=(14, 5), bordercolor=t["line"])
    style.map("TNotebook.Tab",
              background=[("selected", t["bg"])],
              foreground=[("selected", t["accent"])])
    style.configure("TPanedwindow", background=t["bg"])
    style.configure("Sash", background=t["line"], sashthickness=5)
    style.configure("Vertical.TScrollbar", background=t["panel"],
                    troughcolor=t["bg"], bordercolor=t["bg"],
                    arrowcolor=t["muted"])
    style.configure("TSeparator", background=t["line"])
    style.configure("Status.TLabel", background=t["panel"],
                    foreground=t["muted"], padding=(6, 3))
    return style


def bar(done, total, width=12):
    """A little text progress bar, since ttk cannot put one inside a cell."""
    if not total:
        return ""
    filled = max(0, min(width, round(width * done / total)))
    return "█" * filled + "░" * (width - filled) + f" {done * 100 // total:>3}%"
CHUNK = 256 * 1024

CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), APP
)
SITES_FILE = os.path.join(CONFIG_DIR, "sites.json")
SECRETS_FILE = os.path.join(CONFIG_DIR, "secrets.json")


# --------------------------------------------------------------------------
# model
# --------------------------------------------------------------------------

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


# --------------------------------------------------------------------------
# local disk
# --------------------------------------------------------------------------

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


# --------------------------------------------------------------------------
# SFTP, via the OpenSSH client
# --------------------------------------------------------------------------

class SFTPBackend(Backend):
    """
    Talks to the `sftp` binary in batch mode.

    Each operation is a short-lived `sftp -b -` run. That sounds costly, but
    ControlMaster multiplexing keeps every call after the first on the one
    already-open SSH connection, so a listing lands in well under a second.
    Going through the real client (rather than a Python SSH library) is what
    buys us agent forwarding, jump hosts, ~/.ssh/config aliases and hardware
    keys with no code at all.
    """

    scheme = "sftp"

    def __init__(self, host, user=None, port=None, label=None, identity=None):
        self.host = host
        self.user = user or ""
        self.port = port
        self.identity = identity
        self.label = label or f"sftp://{user + '@' if user else ''}{host}"
        # Kept short deliberately: macOS caps unix socket paths at ~104 bytes,
        # and this one has the host and port appended by ssh at connect time.
        self._ctl = os.path.join(
            tempfile.gettempdir(), f".{APP}-{os.getuid()}-%r@%h-%p"
        )
        self._home = None

    # -- plumbing ---------------------------------------------------------

    @property
    def target(self):
        return f"{self.user}@{self.host}" if self.user else self.host

    def _base_args(self, for_ssh=False):
        args = [
            "-o", "BatchMode=yes",
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={self._ctl}",
            "-o", "ControlPersist=120",
            "-o", "ConnectTimeout=15",
        ]
        if self.port:
            args += (["-p", str(self.port)] if for_ssh else ["-P", str(self.port)])
        if self.identity:
            args += ["-i", self.identity]
        return args

    @staticmethod
    def q(path: str) -> str:
        """Quote a path for the sftp command language."""
        return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def _run(self, commands, timeout=120, tolerant=True):
        """Run a batch of sftp commands, returning stdout."""
        if tolerant:
            # A leading '-' tells sftp to keep going past a failing command,
            # so one missing file cannot abort the whole batch.
            commands = [c if c.startswith("-") else "-" + c for c in commands]
        script = "\n".join(commands) + "\n"
        try:
            proc = subprocess.run(
                ["sftp", "-b", "-", *self._base_args(), self.target],
                input=script,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError:
            raise TransferError("the `sftp` command is not installed")
        except subprocess.TimeoutExpired:
            raise TransferError(f"sftp timed out after {timeout}s")
        # sftp reports per-command failures ("Can't ls", "remote open ...
        # failed") on stderr while the batch itself still exits 0, so the
        # two streams have to be read together or errors vanish silently.
        out = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0 and not proc.stdout:
            msg = out.strip().splitlines()
            raise TransferError(msg[-1] if msg else "sftp failed")
        return out

    def connect(self):
        out = self._run(["pwd"], timeout=30, tolerant=False)
        for line in out.splitlines():
            if line.startswith("Remote working directory:"):
                self._home = line.split(":", 1)[1].strip()
        if not self._home:
            raise TransferError(f"could not open an SFTP session to {self.host}")

    def close(self):
        subprocess.run(
            ["ssh", "-O", "exit", "-o", f"ControlPath={self._ctl}", self.target],
            capture_output=True,
        )

    def home(self):
        return self._home or "/"

    # -- operations -------------------------------------------------------

    def listdir(self, path):
        out = self._run([f"ls -la {self.q(path)}"])
        entries = []
        for line in out.splitlines():
            if line.startswith("sftp>") or not line.strip():
                continue
            if line.startswith("Can't ls:") or line.startswith("remote readdir"):
                raise TransferError(line)
            e = self._parse_ls(line, path)
            if e and e.name not in (".", ".."):
                entries.append(e)
        return entries

    @staticmethod
    def _parse_ls(line, base):
        # drwxr-xr-x    ? root  root  4096 Aug 31 17:54 /opt/finance-tracker
        parts = line.split(None, 8)
        if len(parts) < 9 or len(parts[0]) < 10:
            return None
        mode, _links, _owner, _group, size, mon, day, timeyear, name = parts
        if mode[0] not in "dlrwx-bcps":
            return None
        # sftp echoes the full path it was given; show just the leaf.
        name = name.rstrip("/")
        if name.startswith(base.rstrip("/") + "/"):
            name = name[len(base.rstrip("/")) + 1:]
        name = name.split("/")[-1] if base == "/" else name
        return Entry(
            name=name,
            size=0 if mode[0] == "d" else _int(size),
            mtime=_ls_time(mon, day, timeyear),
            is_dir=mode[0] == "d",
            is_link=mode[0] == "l",
            mode=mode,
        )

    def mkdir(self, path):
        self._check(self._run([f"mkdir {self.q(path)}"]), "mkdir")

    def remove(self, path):
        self._check(self._run([f"rm {self.q(path)}"]), "rm")

    def rmdir(self, path):
        # sftp's rmdir only removes empty directories, so recurse ourselves.
        for e in self.listdir(path):
            child = self.join(path, e.name)
            self.rmdir(child) if e.is_dir else self.remove(child)
        self._check(self._run([f"rmdir {self.q(path)}"]), "rmdir")

    def rename(self, src, dst):
        self._check(self._run([f"rename {self.q(src)} {self.q(dst)}"]), "rename")

    def _transfer(self, command, verb, total, watch, progress):
        """
        Run a get/put while reporting progress from the file's own size.

        OpenSSH's progress meter is not usable here - it draws nothing even
        with a pty on both stdin and stdout - so rather than parse a meter
        that may or may not exist, watch the file grow. For a download that
        is a free local stat; for an upload it is one cheap `stat` over the
        already-multiplexed SSH connection.
        """
        result = {}

        def run():
            try:
                result["out"] = self._run([command], timeout=None)
            except Exception as e:                      # noqa: BLE001
                result["err"] = e

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        started = time.monotonic()
        interval = 0.4 if watch is None or total <= 0 else 0.5
        while worker.is_alive():
            worker.join(interval)
            if not (progress and watch):
                continue
            try:
                done = watch()
            except Exception:                           # noqa: BLE001
                continue
            if done < 0:
                continue
            elapsed = time.monotonic() - started
            progress(done, total, done / elapsed if elapsed > 0.5 else 0)

        if "err" in result:
            raise result["err"]
        self._check(result.get("out", ""), verb)
        if progress and total > 0:
            progress(total, total, 0)

    def _remote_size(self, path):
        """One cheap stat over the shared SSH connection, -1 if unavailable."""
        try:
            r = subprocess.run(
                ["ssh", *self._base_args(for_ssh=True), self.target,
                 f"stat -c %s -- {shlex.quote(path)} 2>/dev/null || echo -1"],
                capture_output=True, text=True, timeout=20,
            )
            return int((r.stdout or "-1").strip().split()[-1])
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
            return -1

    def read_to(self, path, local_path, progress=None, resume=False):
        # reget continues a partial local file instead of starting over.
        verb = "reget" if resume and os.path.exists(local_path) else "get"
        total = self.size_of(path)

        def watch():
            try:
                return os.path.getsize(local_path)
            except OSError:
                return 0

        self._transfer(f"-{verb} {self.q(path)} {self.q(local_path)}",
                       verb, total, watch, progress)

    def write_from(self, local_path, path, progress=None, resume=False):
        verb = "reput" if resume and self._remote_size(path) > 0 else "put"
        try:
            total = os.path.getsize(local_path)
        except OSError:
            total = 0
        self._transfer(f"-{verb} {self.q(local_path)} {self.q(path)}",
                       verb, total, lambda: self._remote_size(path), progress)

    def size_of(self, path):
        for line in self._run([f"ls -l {self.q(path)}"]).splitlines():
            e = self._parse_ls(line.strip(), posixpath.dirname(path) or "/")
            if e and not e.is_dir:
                return e.size
        return -1

    def checksum(self, path):
        """
        Hash the file on the far side, so a skip decision costs no transfer.

        Needs a shell on the remote, which a locked-down sftp-only account
        will not have; returning None there simply falls back to a size
        comparison rather than failing the transfer.
        """
        for tool, field in (("sha256sum", 0), ("shasum -a 256", 0)):
            try:
                r = subprocess.run(
                    ["ssh", *self._base_args(for_ssh=True),
                     self.target, f"{tool} -- {shlex.quote(path)}"],
                    capture_output=True, text=True, timeout=900,
                )
            except (OSError, subprocess.TimeoutExpired):
                return None
            if r.returncode == 0 and r.stdout.split():
                return r.stdout.split()[field]
        return None

    # On success sftp prints nothing but its own command echo, so anything
    # else on either stream is a failure. Whitelisting the handful of benign
    # lines beats trying to enumerate every way it can word an error.
    _BENIGN = ("sftp>", "Fetching ", "Uploading ", "Downloading ",
               "Retrieving ", "Remote working directory:", "Changing ",
               "Entering ")

    @classmethod
    def _check(cls, out, what):
        for line in out.splitlines():
            line = line.strip()          # sftp ends some lines with a CR
            if not line or line.startswith(cls._BENIGN):
                continue
            raise TransferError(f"{what}: {line}")


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


# --------------------------------------------------------------------------
# FTP / FTPS
# --------------------------------------------------------------------------

class FTPBackend(Backend):
    scheme = "ftp"
    max_parallel = 1        # one control connection, so strictly serial

    def __init__(self, host, user="anonymous", password="", port=21,
                 tls=False, passive=True, label=None):
        self.host, self.port = host, port or 21
        self.user, self.password = user, password
        self.tls, self.passive = tls, passive
        self.label = label or f"{'ftps' if tls else 'ftp'}://{user}@{host}"
        self.ftp = None

    def connect(self):
        self.ftp = FTP_TLS() if self.tls else FTP()
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


# --------------------------------------------------------------------------
# S3 (SigV4, hand-rolled)
# --------------------------------------------------------------------------

class S3Backend(Backend):
    """
    A bucket presented as a directory tree.

    S3 has no directories, only keys with slashes in them, so a "folder" here
    is a common prefix. Making one writes a zero-byte marker object ending in
    '/', which is the same convention the AWS console uses.
    """

    scheme = "s3"

    def __init__(self, bucket, access_key, secret_key, region="us-east-1",
                 endpoint=None, session_token=None, label=None):
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.session_token = session_token
        self.region = region or "us-east-1"
        # Any S3-compatible store works: MinIO, R2, Backblaze, Wasabi.
        self.endpoint = (endpoint or f"https://s3.{self.region}.amazonaws.com").rstrip("/")
        self.label = label or f"s3://{bucket}"

    def connect(self):
        self._request("GET", "/", query={"list-type": "2", "max-keys": "1"})

    # -- signing ----------------------------------------------------------

    def _request(self, method, key_path, query=None, body=b"", headers=None,
                 stream=False):
        query = query or {}
        headers = dict(headers or {})
        url = urllib.parse.urlparse(self.endpoint)
        host = url.netloc
        # Path-style addressing works with every S3 clone, unlike vhost style.
        canon_path = "/" + self.bucket + _s3_quote(key_path)
        now = dt.datetime.now(dt.timezone.utc)
        amzdate = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(body if isinstance(body, bytes) else b"").hexdigest()

        headers.update({
            "host": host,
            "x-amz-date": amzdate,
            "x-amz-content-sha256": payload_hash,
        })
        if self.session_token:
            headers["x-amz-security-token"] = self.session_token

        signed = sorted(k.lower() for k in headers)
        canon_headers = "".join(f"{k}:{str(headers[k]).strip()}\n" for k in signed)
        signed_headers = ";".join(signed)
        canon_query = "&".join(
            f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(str(v), safe='')}"
            for k, v in sorted(query.items())
        )
        canon_req = "\n".join([method, canon_path, canon_query,
                               canon_headers, signed_headers, payload_hash])
        scope = f"{datestamp}/{self.region}/s3/aws4_request"
        to_sign = "\n".join([
            "AWS4-HMAC-SHA256", amzdate, scope,
            hashlib.sha256(canon_req.encode()).hexdigest(),
        ])
        k = ("AWS4" + self.secret_key).encode()
        for part in (datestamp, self.region, "s3", "aws4_request"):
            k = hmac.new(k, part.encode(), hashlib.sha256).digest()
        sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
        headers["Authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={sig}"
        )

        full = f"{url.scheme}://{host}{canon_path}"
        if canon_query:
            full += "?" + canon_query
        req = urllib.request.Request(full, data=body or None, method=method)
        for hk, hv in headers.items():
            req.add_header(hk, str(hv))
        try:
            resp = urllib.request.urlopen(req, timeout=60)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = ET.fromstring(e.read()).findtext("Message") or ""
            except Exception:
                pass
            raise TransferError(f"S3 {method} {key_path}: {e.code} {detail or e.reason}")
        except urllib.error.URLError as e:
            raise TransferError(f"S3 {method}: {e.reason}")
        return resp if stream else resp.read()

    # -- operations -------------------------------------------------------

    @staticmethod
    def _prefix(path):
        p = path.strip("/")
        return p + "/" if p else ""

    def listdir(self, path):
        prefix = self._prefix(path)
        entries, token = [], None
        NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
        while True:
            q = {"list-type": "2", "prefix": prefix, "delimiter": "/",
                 "max-keys": "1000"}
            if token:
                q["continuation-token"] = token
            root = ET.fromstring(self._request("GET", "/", query=q))
            for cp in root.findall(f"{NS}CommonPrefixes"):
                name = (cp.findtext(f"{NS}Prefix") or "")[len(prefix):].rstrip("/")
                if name:
                    entries.append(Entry(name=name, is_dir=True))
            for obj in root.findall(f"{NS}Contents"):
                key = obj.findtext(f"{NS}Key") or ""
                name = key[len(prefix):]
                if not name or name.endswith("/"):
                    continue           # the folder marker object itself
                entries.append(Entry(
                    name=name,
                    size=_int(obj.findtext(f"{NS}Size")),
                    mtime=_iso_time(obj.findtext(f"{NS}LastModified")),
                ))
            if (root.findtext(f"{NS}IsTruncated") or "").lower() != "true":
                break
            token = root.findtext(f"{NS}NextContinuationToken")
        return entries

    def mkdir(self, path):
        self._request("PUT", "/" + self._prefix(path), body=b"")

    def remove(self, path):
        self._request("DELETE", "/" + path.lstrip("/"))

    def rmdir(self, path):
        for e in self.listdir(path):
            child = self.join(path, e.name)
            self.rmdir(child) if e.is_dir else self.remove(child)
        try:
            self._request("DELETE", "/" + self._prefix(path))
        except TransferError:
            pass                        # no marker object; nothing to clean up

    def rename(self, src, dst):
        # S3 cannot move; copy server-side then delete the original.
        self._request("PUT", "/" + dst.lstrip("/"), headers={
            "x-amz-copy-source": f"/{self.bucket}{_s3_quote('/' + src.lstrip('/'))}"
        })
        self.remove(src)

    def read_to(self, path, local_path, progress=None):
        resp = self._request("GET", "/" + path.lstrip("/"), stream=True)
        total = _int(resp.headers.get("Content-Length"))
        with open(local_path, "wb") as fh:
            _pump(resp, fh, total, progress)

    def write_from(self, local_path, path, progress=None):
        # Single PUT: fine to a few GB, which is the scope of this tool.
        with open(local_path, "rb") as fh:
            body = fh.read()
        if progress:
            progress(0, len(body), 0)
        self._request("PUT", "/" + path.lstrip("/"), body=body,
                      headers={"content-length": str(len(body))})
        if progress:
            progress(len(body), len(body), 0)


def _s3_quote(path):
    return urllib.parse.quote(path, safe="/~")


def _iso_time(s):
    try:
        return dt.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=dt.timezone.utc).timestamp()
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------
# WebDAV
# --------------------------------------------------------------------------

class WebDAVBackend(Backend):
    scheme = "dav"

    def __init__(self, url, user="", password="", label=None):
        self.base = url.rstrip("/")
        parsed = urllib.parse.urlparse(self.base)
        self.root = parsed.path.rstrip("/")     # e.g. /remote.php/dav/files/me
        self.user, self.password = user, password
        self.label = label or f"dav:{parsed.netloc}"

    def connect(self):
        self.listdir("/")

    def _url(self, path):
        return self.base + urllib.parse.quote(path if path.startswith("/") else "/" + path)

    def _request(self, method, path, body=None, headers=None, stream=False):
        req = urllib.request.Request(self._url(path), data=body, method=method)
        if self.user:
            token = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
            req.add_header("Authorization", "Basic " + token)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            resp = urllib.request.urlopen(req, timeout=60)
        except urllib.error.HTTPError as e:
            if e.code in (207,):
                return e
            raise TransferError(f"WebDAV {method} {path}: {e.code} {e.reason}")
        except urllib.error.URLError as e:
            raise TransferError(f"WebDAV {method}: {e.reason}")
        return resp if stream else resp

    def listdir(self, path):
        body = (
            '<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop>'
            "<d:resourcetype/><d:getcontentlength/><d:getlastmodified/>"
            "</d:prop></d:propfind>"
        ).encode()
        resp = self._request("PROPFIND", path, body=body,
                             headers={"Depth": "1",
                                      "Content-Type": 'application/xml; charset="utf-8"'})
        root = ET.fromstring(resp.read())
        here = urllib.parse.unquote(self.root + "/" + path.strip("/")).rstrip("/")
        entries = []
        for r in root.findall("{DAV:}response"):
            href = urllib.parse.unquote(r.findtext("{DAV:}href") or "").rstrip("/")
            if href == here or not href:
                continue                # the collection we asked about
            name = href.split("/")[-1]
            props = r.find("{DAV:}propstat/{DAV:}prop")
            if props is None:
                continue
            is_dir = props.find("{DAV:}resourcetype/{DAV:}collection") is not None
            entries.append(Entry(
                name=name,
                size=_int(props.findtext("{DAV:}getcontentlength")),
                mtime=_http_time(props.findtext("{DAV:}getlastmodified")),
                is_dir=is_dir,
            ))
        return entries

    def mkdir(self, path):
        self._request("MKCOL", path)

    def remove(self, path):
        self._request("DELETE", path)

    def rmdir(self, path):
        self._request("DELETE", path)   # DELETE on a collection is recursive

    def rename(self, src, dst):
        self._request("MOVE", src, headers={"Destination": self._url(dst),
                                            "Overwrite": "T"})

    def read_to(self, path, local_path, progress=None):
        resp = self._request("GET", path, stream=True)
        total = _int(resp.headers.get("Content-Length"))
        with open(local_path, "wb") as fh:
            _pump(resp, fh, total, progress)

    def write_from(self, local_path, path, progress=None):
        total = os.path.getsize(local_path)
        with open(local_path, "rb") as fh:
            body = fh.read()
        if progress:
            progress(0, total, 0)
        self._request("PUT", path, body=body,
                      headers={"Content-Type": "application/octet-stream"})
        if progress:
            progress(total, total, 0)


def _http_time(s):
    try:
        return dt.datetime.strptime(s, "%a, %d %b %Y %H:%M:%S %Z").replace(
            tzinfo=dt.timezone.utc).timestamp()
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------
# sites
# --------------------------------------------------------------------------

DEFAULT_SITES = [
    {"name": "Local", "type": "local"},
]


def load_sites():
    try:
        with open(SITES_FILE) as fh:
            sites = json.load(fh)
        if isinstance(sites, dict):
            sites = sites.get("sites", [])
        return [s for s in sites if isinstance(s, dict) and s.get("name")]
    except (OSError, ValueError):
        return list(DEFAULT_SITES)


def save_sites(sites):
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    tmp = SITES_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(sites, fh, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, SITES_FILE)


def load_secrets():
    try:
        with open(SECRETS_FILE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_secret(name, value):
    """
    Stash a password so a site connects without prompting.

    This is obfuscation, not encryption - the file is plain JSON at mode 0600.
    Nothing is ever written here unless you explicitly tick 'remember'.
    """
    secrets = load_secrets()
    secrets[name] = value
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    tmp = SECRETS_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(secrets, fh, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, SECRETS_FILE)


def needs_password(site):
    return site.get("type") in ("ftp", "dav") and not site.get("anonymous")


def make_backend(site, password=""):
    kind = site.get("type", "local")
    name = site.get("name")
    if kind == "local":
        return LocalBackend(label=name or "Local", start=site.get("path"))
    if kind == "sftp":
        return SFTPBackend(
            host=site["host"], user=site.get("user"), port=site.get("port"),
            identity=site.get("identity"), label=name,
        )
    if kind in ("ftp", "ftps"):
        return FTPBackend(
            host=site["host"], user=site.get("user") or "anonymous",
            password=password, port=site.get("port", 21),
            tls=(kind == "ftps" or bool(site.get("tls"))),
            passive=site.get("passive", True), label=name,
        )
    if kind == "s3":
        return S3Backend(
            bucket=site["bucket"],
            access_key=site.get("access_key") or os.environ.get("AWS_ACCESS_KEY_ID", ""),
            secret_key=site.get("secret_key") or os.environ.get("AWS_SECRET_ACCESS_KEY", ""),
            region=site.get("region", "us-east-1"),
            endpoint=site.get("endpoint"),
            session_token=site.get("session_token") or os.environ.get("AWS_SESSION_TOKEN"),
            label=name,
        )
    if kind in ("dav", "webdav"):
        return WebDAVBackend(url=site["url"], user=site.get("user", ""),
                             password=password, label=name)
    raise TransferError(f"unknown site type: {kind!r}")


# --------------------------------------------------------------------------
# transfer queue
# --------------------------------------------------------------------------

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

    def _expand(self, job: Job):
        """Turn a directory into child jobs, queued right behind it."""
        try:
            job.dst.mkdir(job.dst_path)
        except Exception:                                # noqa: BLE001
            pass                                         # already there
        children = [
            Job(src=job.src, src_path=job.src.join(job.src_path, e.name),
                dst=job.dst, dst_path=posixpath.join(job.dst_path, e.name),
                is_dir=e.is_dir, size=e.size)
            for e in job.src.listdir(job.src_path)
        ]
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


# --------------------------------------------------------------------------
# formatting helpers
# --------------------------------------------------------------------------

def human(n):
    if not n:
        return ""
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0


def when(ts):
    if not ts:
        return ""
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk


class Pane(ttk.Frame):
    """One side of the window: a site picker, a path bar and a file list."""

    def __init__(self, master, app, title, initial=None):
        super().__init__(master, padding=(4, 4))
        self.app = app
        self.backend: Backend | None = None
        self.path = "/"
        self.entries: list[Entry] = []

        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Label(bar, text=title, width=7).pack(side="left")
        self.site_var = tk.StringVar()
        self.site_box = ttk.Combobox(bar, textvariable=self.site_var,
                                     state="readonly", width=18)
        self.site_box.pack(side="left", padx=(0, 4))
        self.site_box.bind("<<ComboboxSelected>>", lambda e: self.connect())
        ttk.Button(bar, text="Connect", width=8,
                   command=self.connect).pack(side="left")

        nav = ttk.Frame(self)
        nav.pack(fill="x", pady=(4, 4))
        ttk.Button(nav, text="↑", width=3, command=self.go_up).pack(side="left")
        ttk.Button(nav, text="⌂", width=3, command=self.go_home).pack(side="left")
        ttk.Button(nav, text="↻", width=3, command=self.refresh).pack(side="left")
        self.path_var = tk.StringVar()
        pe = ttk.Entry(nav, textvariable=self.path_var)
        pe.pack(side="left", fill="x", expand=True, padx=4)
        pe.bind("<Return>", lambda e: self.chdir(self.path_var.get()))

        cols = ("size", "modified")
        self.tree = ttk.Treeview(self, columns=cols, selectmode="extended")
        self.tree.heading("#0", text="Name",
                          command=lambda: self.sort_by("name"))
        self.tree.heading("size", text="Size",
                          command=lambda: self.sort_by("size"))
        self.tree.heading("modified", text="Modified",
                          command=lambda: self.sort_by("mtime"))
        self.tree.column("#0", width=240, stretch=True)
        self.tree.column("size", width=70, anchor="e", stretch=False)
        self.tree.column("modified", width=125, anchor="w", stretch=False)
        vs = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")

        self.tree.bind("<Double-1>", self.on_open)
        self.tree.bind("<Return>", self.on_open)
        self.tree.bind("<Delete>", lambda e: self.delete_selected())
        self.tree.bind("<F2>", lambda e: self.rename_selected())
        self.tree.bind("<Button-3>", self.on_menu)

        self.menu = tk.Menu(self, tearoff=0, bg=THEME["panel"],
                            fg=THEME["text"], activebackground=THEME["accent"],
                            activeforeground=THEME["bg"], borderwidth=0)
        self.menu.add_command(label="Transfer →", command=lambda: app.transfer(self))
        self.menu.add_separator()
        self.menu.add_command(label="New folder…", command=self.new_folder)
        self.menu.add_command(label="Rename…  (F2)", command=self.rename_selected)
        self.menu.add_command(label="Delete   (Del)", command=self.delete_selected)
        self.menu.add_separator()
        self.menu.add_command(label="Refresh", command=self.refresh)

        self.sort_field = "name"
        self.status = ttk.Label(self, text="not connected", anchor="w")
        self.status.pack(side="bottom", fill="x", before=self.tree)

        self.reload_sites(initial)

    # -- sites ------------------------------------------------------------

    def reload_sites(self, select=None):
        names = [s["name"] for s in self.app.sites]
        self.site_box["values"] = names
        if select and select in names:
            self.site_var.set(select)
        elif names and not self.site_var.get():
            self.site_var.set(names[0])

    def connect(self):
        name = self.site_var.get()
        site = next((s for s in self.app.sites if s["name"] == name), None)
        if not site:
            return
        password = ""
        if needs_password(site):
            password = load_secrets().get(name, "")
            if not password:
                password = simpledialog.askstring(
                    "Password", f"Password for {name}:", show="•",
                    parent=self.app.root) or ""
        self.set_status(f"connecting to {name}…")
        old = self.backend

        def work():
            backend = make_backend(site, password)
            backend.connect()
            return backend

        def ok(backend):
            if old:
                threading.Thread(target=old.close, daemon=True).start()
            self.backend = backend
            self.app.log(f"connected: {backend.label}")
            self.chdir(backend.home())

        self.app.run_async(work, ok, self.fail)

    # -- navigation -------------------------------------------------------

    def chdir(self, path):
        if not self.backend:
            self.set_status("pick a site and hit Connect")
            return
        path = path or "/"
        self.set_status(f"reading {path}…")

        def work():
            return self.backend.listdir(path)

        def ok(entries):
            self.path = path
            self.path_var.set(path)
            self.entries = entries
            self.render()

        self.app.run_async(work, ok, self.fail)

    def go_up(self):
        if self.backend:
            self.chdir(self.backend.parent(self.path))

    def go_home(self):
        if self.backend:
            self.chdir(self.backend.home())

    def refresh(self):
        self.chdir(self.path)

    def on_open(self, _event=None):
        for e in self.selection():
            if e.is_dir or e.is_link:
                self.chdir(self.backend.join(self.path, e.name))
            return

    def on_menu(self, event):
        row = self.tree.identify_row(event.y)
        if row and row not in self.tree.selection():
            self.tree.selection_set(row)
        self.app.focus_pane = self
        self.menu.tk_popup(event.x_root, event.y_root)

    # -- rendering --------------------------------------------------------

    def sort_by(self, field):
        self.sort_field = field
        self.render()

    def render(self):
        self.tree.delete(*self.tree.get_children())
        if self.sort_field == "name":
            rows = sorted(self.entries, key=lambda e: e.sort_key)
        else:
            rows = sorted(self.entries,
                          key=lambda e: (not e.is_dir,
                                         -getattr(e, self.sort_field, 0)))
        for e in rows:
            icon = "\U0001f4c1 " if e.is_dir else ("\U0001f517 " if e.is_link else "\U0001f4c4 ")
            self.tree.insert("", "end", iid=e.name, text=icon + e.name,
                             values=(human(e.size), when(e.mtime)))
        ndirs = sum(1 for e in self.entries if e.is_dir)
        total = sum(e.size for e in self.entries if not e.is_dir)
        self.set_status(
            f"{len(self.entries) - ndirs} files, {ndirs} folders"
            + (f" — {human(total)}" if total else "")
        )

    def selection(self) -> list[Entry]:
        by_name = {e.name: e for e in self.entries}
        return [by_name[n] for n in self.tree.selection() if n in by_name]

    def set_status(self, text):
        self.status.configure(text=text)

    def fail(self, exc):
        self.set_status(f"error: {exc}")
        self.app.log(f"[{self.site_var.get()}] {exc}")

    # -- file operations --------------------------------------------------

    def new_folder(self):
        if not self.backend:
            return
        name = simpledialog.askstring("New folder", "Name:", parent=self.app.root)
        if not name:
            return
        target = self.backend.join(self.path, name)
        self.app.run_async(lambda: self.backend.mkdir(target),
                           lambda _: self.refresh(), self.fail)

    def rename_selected(self):
        sel = self.selection()
        if not self.backend or not sel:
            return
        old = sel[0].name
        new = simpledialog.askstring("Rename", "New name:", initialvalue=old,
                                     parent=self.app.root)
        if not new or new == old:
            return
        src = self.backend.join(self.path, old)
        dst = self.backend.join(self.path, new)
        self.app.run_async(lambda: self.backend.rename(src, dst),
                           lambda _: self.refresh(), self.fail)

    def delete_selected(self):
        sel = self.selection()
        if not self.backend or not sel:
            return
        what = sel[0].name if len(sel) == 1 else f"{len(sel)} items"
        if not messagebox.askyesno("Delete", f"Delete {what} from "
                                             f"{self.backend.label}?\n"
                                             "Folders are removed with their contents.",
                                   parent=self.app.root):
            return

        def work():
            for e in sel:
                target = self.backend.join(self.path, e.name)
                self.backend.rmdir(target) if e.is_dir else self.backend.remove(target)

        self.app.run_async(work, lambda _: self.refresh(), self.fail)


class SiteDialog(tk.Toplevel):
    """Add or edit a saved site. Fields shown depend on the protocol."""

    FIELDS = {
        "local": [("path", "Start folder (blank = home)")],
        "sftp": [("host", "Host or ~/.ssh/config alias"), ("user", "User"),
                 ("port", "Port (22)"), ("identity", "Key file (optional)")],
        "ftp": [("host", "Host"), ("user", "User"), ("port", "Port (21)")],
        "ftps": [("host", "Host"), ("user", "User"), ("port", "Port (21)")],
        "s3": [("bucket", "Bucket"), ("region", "Region"),
               ("endpoint", "Endpoint (blank = AWS)"),
               ("access_key", "Access key"), ("secret_key", "Secret key")],
        "dav": [("url", "Base URL"), ("user", "User")],
    }

    def __init__(self, parent, site=None):
        super().__init__(parent)
        self.title("Site")
        self.transient(parent)
        self.result = None
        self.site = dict(site or {"type": "sftp"})
        self.vars = {}

        top = ttk.Frame(self, padding=10)
        top.pack(fill="both", expand=True)
        ttk.Label(top, text="Name").grid(row=0, column=0, sticky="w", pady=2)
        self.name_var = tk.StringVar(value=self.site.get("name", ""))
        ttk.Entry(top, textvariable=self.name_var, width=32).grid(
            row=0, column=1, sticky="ew", pady=2)

        ttk.Label(top, text="Protocol").grid(row=1, column=0, sticky="w", pady=2)
        self.type_var = tk.StringVar(value=self.site.get("type", "sftp"))
        box = ttk.Combobox(top, textvariable=self.type_var, state="readonly",
                           values=list(self.FIELDS), width=30)
        box.grid(row=1, column=1, sticky="ew", pady=2)
        box.bind("<<ComboboxSelected>>", lambda e: self.build_fields())

        self.body = ttk.Frame(top)
        self.body.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        top.columnconfigure(1, weight=1)

        btns = ttk.Frame(top)
        btns.grid(row=3, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="Save", command=self.save).pack(side="right", padx=4)

        self.build_fields()
        self.grab_set()
        self.wait_window(self)

    def build_fields(self):
        for w in self.body.winfo_children():
            w.destroy()
        self.vars.clear()
        for i, (key, caption) in enumerate(self.FIELDS[self.type_var.get()]):
            ttk.Label(self.body, text=caption).grid(row=i, column=0,
                                                    sticky="w", pady=2)
            var = tk.StringVar(value=str(self.site.get(key, "")))
            show = "•" if key == "secret_key" else ""
            ttk.Entry(self.body, textvariable=var, width=32, show=show).grid(
                row=i, column=1, sticky="ew", pady=2, padx=(8, 0))
            self.vars[key] = var
        self.body.columnconfigure(1, weight=1)

    def save(self):
        name = self.name_var.get().strip()
        if not name:
            messagebox.showerror("Site", "Give the site a name.", parent=self)
            return
        out = {"name": name, "type": self.type_var.get()}
        for key, var in self.vars.items():
            val = var.get().strip()
            if val:
                out[key] = int(val) if key == "port" and val.isdigit() else val
        self.result = out
        self.destroy()


class App:
    def __init__(self, root):
        self.root = root
        # Applied here rather than in main() so anything embedding App - the
        # tests included - gets the same window instead of default grey.
        self.style = apply_theme(root)
        root.title(f"Foxzilla {VERSION}")
        root.geometry("1100x680")
        self.sites = load_sites()
        self.focus_pane = None
        self._ui_queue: queue.Queue = queue.Queue()
        self._pending_refresh = None
        self._rows: dict[str, Job] = {}
        self.queue = Queue(self.on_job_change, workers=8)

        self._build_menu()

        outer = ttk.Frame(root)
        outer.pack(fill="both", expand=True)

        split = ttk.PanedWindow(outer, orient="horizontal")
        split.pack(fill="both", expand=True, padx=4, pady=4)
        self.left = Pane(split, self, "Local", initial="Local")
        self.right = Pane(split, self, "Remote",
                          initial=next((s["name"] for s in self.sites
                                        if s.get("type") != "local"), None))
        split.add(self.left, weight=1)
        split.add(self.right, weight=1)

        for pane in (self.left, self.right):
            pane.tree.bind("<FocusIn>",
                           lambda e, p=pane: setattr(self, "focus_pane", p))

        mid = ttk.Frame(outer)
        mid.pack(fill="x", padx=8, pady=(2, 0))
        ttk.Button(mid, text="→  Send", width=10, style="Accent.TButton",
                   command=lambda: self.transfer(self.left)).pack(side="left")
        ttk.Button(mid, text="←  Fetch", width=10, style="Accent.TButton",
                   command=lambda: self.transfer(self.right)).pack(side="left", padx=4)

        self.skip_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(mid, text="Skip files already there",
                        variable=self.skip_var,
                        command=lambda: setattr(self.queue, "skip_identical",
                                                self.skip_var.get())
                        ).pack(side="left", padx=(16, 0))

        ttk.Label(mid, text="parallel").pack(side="left", padx=(16, 4))
        self.workers_var = tk.StringVar(value="3")
        wbox = ttk.Combobox(mid, textvariable=self.workers_var, state="readonly",
                            width=3, values=("1", "2", "3", "4", "6", "8"))
        wbox.pack(side="left")
        wbox.bind("<<ComboboxSelected>>",
                  lambda e: self.queue.set_limit(self.workers_var.get()))

        ttk.Button(mid, text="Clear finished",
                   command=self.clear_finished).pack(side="right")
        ttk.Button(mid, text="Cancel all",
                   command=self.queue.cancel_all).pack(side="right", padx=4)
        self.pause_btn = ttk.Button(mid, text="Pause", width=8,
                                    command=self.toggle_pause)
        self.pause_btn.pack(side="right", padx=4)

        tabs = ttk.Notebook(outer, height=200)
        tabs.pack(fill="both", expand=False, padx=4, pady=4)

        qf = ttk.Frame(tabs)
        qbar = ttk.Frame(qf)
        qbar.pack(side="top", fill="x", pady=(0, 2))
        ttk.Label(qbar, text="priority").pack(side="left", padx=(2, 6))
        for text, fn in (("⤒ Top", lambda: self.reorder("top")),
                         ("↑", lambda: self.reorder(-1)),
                         ("↓", lambda: self.reorder(1)),
                         ("⤓ Bottom", lambda: self.reorder("bottom"))):
            ttk.Button(qbar, text=text, width=8 if len(text) > 2 else 3,
                       command=fn).pack(side="left", padx=1)
        ttk.Button(qbar, text="Cancel", width=8,
                   command=self.cancel_selected).pack(side="left", padx=(10, 0))
        ttk.Label(qbar, text="  (or Alt+↑ / Alt+↓ on a row)",
                  style="Hint.TLabel").pack(side="left")

        cols = ("name", "from", "to", "size", "progress", "speed", "state")
        self.qtree = ttk.Treeview(qf, columns=cols, show="headings")
        widths = (170, 190, 190, 70, 130, 80, 110)
        for c, w in zip(cols, widths):
            self.qtree.heading(c, text=c.capitalize())
            self.qtree.column(c, width=w, anchor="e" if c in ("size", "speed") else "w")
        qs = ttk.Scrollbar(qf, orient="vertical", command=self.qtree.yview)
        self.qtree.configure(yscrollcommand=qs.set)
        self.qtree.pack(side="left", fill="both", expand=True)
        qs.pack(side="right", fill="y")
        self.qtree.bind("<Alt-Up>", lambda e: self.reorder(-1))
        self.qtree.bind("<Alt-Down>", lambda e: self.reorder(1))
        self.qtree.bind("<Delete>", lambda e: self.cancel_selected())
        for tag, colour in (("running", THEME["accent"]), ("done", THEME["ok"]),
                            ("failed", THEME["err"]), ("skipped", THEME["muted"]),
                            ("cancelled", THEME["muted"])):
            self.qtree.tag_configure(tag, foreground=colour)
        tabs.add(qf, text="Queue")

        lf = ttk.Frame(tabs)
        self.logbox = tk.Text(lf, height=8, wrap="none", state="disabled",
                              font=("monospace", 9), relief="flat",
                              bg=THEME["field"], fg=THEME["text"],
                              insertbackground=THEME["text"],
                              selectbackground=THEME["sel"])
        ls = ttk.Scrollbar(lf, orient="vertical", command=self.logbox.yview)
        self.logbox.configure(yscrollcommand=ls.set)
        self.logbox.pack(side="left", fill="both", expand=True)
        ls.pack(side="right", fill="y")
        tabs.add(lf, text="Log")

        self.status = ttk.Label(root, anchor="w", style="Status.TLabel",
                                text=f"{APP} {VERSION} — SFTP · FTP/FTPS · S3 · WebDAV")
        self.status.pack(fill="x", side="bottom")

        root.protocol("WM_DELETE_WINDOW", self.quit)
        self._pump()
        self.left.connect()

    # -- menu -------------------------------------------------------------

    def _build_menu(self):
        menubar = tk.Menu(self.root, bg=THEME["panel"], fg=THEME["text"],
                      activebackground=THEME["accent"],
                      activeforeground=THEME["bg"], borderwidth=0)
        sites = tk.Menu(menubar, tearoff=0, bg=THEME["panel"], fg=THEME["text"],
                        activebackground=THEME["accent"],
                        activeforeground=THEME["bg"], borderwidth=0)
        sites.add_command(label="Add site…", command=self.add_site)
        sites.add_command(label="Edit site…", command=self.edit_site)
        sites.add_command(label="Remove site", command=self.remove_site)
        sites.add_separator()
        sites.add_command(label=f"Open {SITES_FILE}", command=self.reveal_config)
        sites.add_separator()
        sites.add_command(label="Quit", command=self.quit)
        menubar.add_cascade(label="Sites", menu=sites)
        self.root.config(menu=menubar)

    def reveal_config(self):
        save_sites(self.sites)
        self.log(f"sites file: {SITES_FILE}")

    def add_site(self):
        dlg = SiteDialog(self.root)
        if dlg.result:
            self.sites.append(dlg.result)
            self._sites_changed(dlg.result["name"])

    def edit_site(self):
        pane = self.focus_pane or self.right
        name = pane.site_var.get()
        idx = next((i for i, s in enumerate(self.sites) if s["name"] == name), None)
        if idx is None:
            return
        dlg = SiteDialog(self.root, self.sites[idx])
        if dlg.result:
            self.sites[idx] = dlg.result
            self._sites_changed(dlg.result["name"])

    def remove_site(self):
        pane = self.focus_pane or self.right
        name = pane.site_var.get()
        if name and messagebox.askyesno("Remove site", f"Remove {name}?"):
            self.sites = [s for s in self.sites if s["name"] != name]
            self._sites_changed()

    def _sites_changed(self, select=None):
        save_sites(self.sites)
        self.left.reload_sites()
        self.right.reload_sites(select)
        self.log("sites saved")

    # -- async plumbing ---------------------------------------------------

    def _pump(self):
        """
        Drain callbacks posted by worker threads.

        Tk objects may only be touched from the thread running the main loop,
        and that includes `after` itself, so background work hands results
        back through a plain queue that this main-thread tick drains.
        """
        for _ in range(64):             # bounded, so the UI stays responsive
            try:
                fn, arg = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn(arg)
            except Exception as e:
                self.log(f"ui error: {e}")
        self.root.after(40, self._pump)

    def post(self, fn, arg=None):
        """Ask the UI thread to run fn(arg). Safe from any thread."""
        self._ui_queue.put((fn, arg))

    def run_async(self, work, on_ok, on_err):
        """Run `work` off the UI thread, then hand the result back on it."""
        def runner():
            try:
                result = work()
            except Exception as e:
                self.post(on_err, e)
            else:
                self.post(on_ok, result)
        threading.Thread(target=runner, daemon=True).start()

    def log(self, text):
        stamp = time.strftime("%H:%M:%S")
        self.logbox.configure(state="normal")
        self.logbox.insert("end", f"{stamp}  {text}\n")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")
        self.status.configure(text=text)

    # -- transfers --------------------------------------------------------

    def transfer(self, src_pane: Pane):
        dst_pane = self.right if src_pane is self.left else self.left
        if not src_pane.backend or not dst_pane.backend:
            messagebox.showinfo(APP, "Connect both panes first.")
            return
        sel = src_pane.selection()
        if not sel:
            messagebox.showinfo(APP, "Nothing selected.")
            return
        for e in sel:
            self.queue.add(Job(
                src=src_pane.backend,
                src_path=src_pane.backend.join(src_pane.path, e.name),
                dst=dst_pane.backend,
                dst_path=posixpath.join(dst_pane.path, e.name),
                is_dir=e.is_dir,
                size=e.size,
            ))
        self.log(f"queued {len(sel)} item(s) → {dst_pane.backend.label}")
        self._pending_refresh = dst_pane

    def on_job_change(self, job: Job):
        # Called from the transfer worker; bounce onto the UI thread.
        self.post(self._render_job, job)

    def _render_job(self, job: Job):
        state, tag = job.state, job.state
        if job.state == "running":
            state = "transferring"
        elif job.state == "failed":
            state = f"failed: {job.error[:40]}"
        elif job.state == "skipped":
            state = "skipped (already there)"
        elif job.state == "done" and job.is_dir:
            state = "expanded"

        values = (
            job.name,
            f"{job.src.label}:{posixpath.dirname(job.src_path) or '/'}",
            f"{job.dst.label}:{posixpath.dirname(job.dst_path) or '/'}",
            "" if job.is_dir else human(job.size),
            "" if job.is_dir else bar(job.done, job.size),
            f"{human(int(job.rate))}/s" if job.rate else "",
            state,
        )
        if job.row and self.qtree.exists(job.row):
            self.qtree.item(job.row, values=values, tags=(tag,))
        else:
            job.row = self.qtree.insert("", "end", values=values, tags=(tag,))
            self._rows[job.row] = job
            self.qtree.see(job.row)

        if job.state in ("done", "failed", "skipped", "cancelled"):
            if job.state == "failed":
                self.log(f"failed {job.name}: {job.error}")
            elif job.state == "skipped" and job.error:
                self.log(f"skipped {job.name} — {job.error}")
            if self.queue.idle():
                pane = self._pending_refresh
                if pane:
                    pane.refresh()
                    self._pending_refresh = None
                self.log("queue idle")

    # -- priority ---------------------------------------------------------

    def _selected_jobs(self):
        return [self._rows[r] for r in self.qtree.selection() if r in self._rows]

    def reorder(self, where):
        """Shift the selected rows through the queue. Order is priority."""
        jobs = self._selected_jobs()
        if not jobs:
            return
        if where == "top":
            for j in reversed(jobs):
                self.queue.move_to(j, 0)
        elif where == "bottom":
            for j in jobs:
                self.queue.move_to(j, len(self.queue.jobs))
        else:
            # moving down: walk from the bottom so they cannot collide
            for j in (jobs if where < 0 else reversed(jobs)):
                self.queue.move(j, where)
        self._resync_queue_view()

    def _resync_queue_view(self):
        """Redraw queue rows in the queue's own order."""
        for pos, job in enumerate(self.queue.jobs):
            if job.row and self.qtree.exists(job.row):
                self.qtree.move(job.row, "", pos)

    def cancel_selected(self):
        for job in self._selected_jobs():
            self.queue.cancel(job)

    def toggle_pause(self):
        paused = not self.queue.paused
        self.queue.set_paused(paused)
        self.pause_btn.configure(text="Resume" if paused else "Pause")
        self.log("queue paused" if paused else "queue resumed")

    def clear_finished(self):
        for job in list(self.queue.jobs):
            if job.state in ("done", "cancelled", "skipped") and job.row \
                    and self.qtree.exists(job.row):
                self.qtree.delete(job.row)
                self._rows.pop(job.row, None)
                self.queue.jobs.remove(job)

    def quit(self):
        for pane in (self.left, self.right):
            if pane.backend:
                try:
                    pane.backend.close()
                except Exception:
                    pass
        self.root.destroy()


def main():
    if not os.path.exists(SITES_FILE):
        save_sites(DEFAULT_SITES)
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
