"""SFTP, via the OpenSSH client."""
from __future__ import annotations

import atexit
import fcntl
import os
import posixpath
import pty
import re
import select
import shlex
import shutil
import subprocess
import tempfile
import termios
import threading
import time

from ..util import APP
from .base import Backend, Entry, TransferError, _int, _ls_time


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

    def __init__(self, host, user=None, port=None, label=None, identity=None,
                 password="", start=None, known_hosts=None):
        self.host = host
        self.user = user or ""
        self.port = port
        self.identity = identity
        # A site can pin the directory it opens in, which is how a deep drop
        # folder becomes one click rather than five.
        self.start = start
        # Somewhere to record the host key. The desktop client wants the
        # user's own known_hosts; a shared service must NOT, or the first
        # visitor to reach a host decides for everyone after them.
        self.known_hosts = known_hosts
        # When set, authentication happens over a pty because OpenSSH will
        # only read a password from a terminal - never from a pipe, and never
        # under BatchMode.
        self.password = password or ""
        self.transcript: list[str] = []
        self.label = label or f"sftp://{user + '@' if user else ''}{host}"
        # A transfer whose byte count has not moved for this long is killed.
        # Keepalives catch a dead connection; this catches a live one that
        # has stopped moving data. None or 0 turns it off.
        self.stall_seconds = self.STALL_SECONDS
        self._ctl_dir = None
        self._ctl = ""
        self._new_ctl_dir()
        self._home = None

    STALL_SECONDS = 300

    def _new_ctl_dir(self):
        """
        A private (0700) directory for this backend's ControlMaster socket.

        Private, so no other backend - or other user of a shared machine -
        can ride this one's authenticated connection; the old fixed
        /tmp/.foxzilla-<uid>-%r@%h-%p name let any second connection to the
        same user@host reuse it, whatever password it was given.

        Kept short deliberately: a unix socket path is capped near 104 bytes,
        so this prefers /tmp to macOS's long per-user temp directory, and
        names the socket %C (a fixed-length hash of user, host and port).
        """
        base = "/tmp" if os.path.isdir("/tmp") and os.access("/tmp", os.W_OK) \
            else tempfile.gettempdir()
        self._ctl_dir = tempfile.mkdtemp(prefix=f"{APP}-", dir=base)
        self._ctl = os.path.join(self._ctl_dir, "%C")
        atexit.register(shutil.rmtree, self._ctl_dir, True)

    def _ensure_ctl_dir(self):
        if not self._ctl_dir or not os.path.isdir(self._ctl_dir):
            self._new_ctl_dir()

    # -- plumbing ---------------------------------------------------------

    @property
    def target(self):
        return f"{self.user}@{self.host}" if self.user else self.host

    def _base_args(self, for_ssh=False):
        self._ensure_ctl_dir()
        args = [
            # BatchMode must be off for a password prompt to happen at all.
            "-o", "BatchMode=no" if self.password else "BatchMode=yes",
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={self._ctl}",
            "-o", "ControlPersist=120",
            "-o", "ConnectTimeout=15",
            # Without these a dropped connection hangs a transfer forever:
            # TCP alone can take hours to notice. One minute of silence ends it.
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=4",
            # Trust on first use, the same bargain a GUI client strikes when
            # it shows a host-key dialog: an unknown host is pinned on first
            # sight, a key that has *changed* is still refused outright. The
            # alternative under BatchMode is a bare "Connection closed" on
            # every first connection, which tells the user nothing.
            "-o", "StrictHostKeyChecking=accept-new",
        ]
        if self.known_hosts:
            args += ["-o", f"UserKnownHostsFile={self.known_hosts}",
                     "-o", "GlobalKnownHostsFile=/dev/null"]
        if self.password:
            args += [
                "-o", "NumberOfPasswordPrompts=1",
                "-o", "PubkeyAuthentication=no",
                "-o", "PreferredAuthentications=password,keyboard-interactive",
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

    @staticmethod
    def _own_the_tty():
        """
        Run in the child between fork and exec: make the pty its *controlling*
        terminal, not merely its stdin.

        OpenSSH opens /dev/tty to ask for a password. On Linux it falls back to
        stdin when there is no controlling terminal, which is why this worked
        without it; macOS does not, so the prompt never appeared, the password
        was never sent, and the server saw a client that disconnected during
        authentication.
        """
        try:
            os.setsid()
        except OSError:
            pass                                 # already a session leader
        try:
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        except OSError:
            pass                                 # best effort; stdin still works

    _PROMPT = re.compile(rb"(?i)(password|passcode|verification code)\s*:\s*\Z")
    # The prompt arrives with no trailing newline and servers word it
    # differently, so also match it anywhere in the chunk just read.
    _PROMPT_ANY = re.compile(rb"(?i)(password|passcode|verification code)\s*:")

    def _run_with_password(self, script, timeout, abort=None):
        """
        Drive sftp interactively so it will actually ask for a password.

        Not with `-b`: that turns on batch mode, and batch mode suppresses the
        password prompt outright — sftp goes straight to "Permission denied"
        without ever asking. So the commands go in over the terminal after
        authentication, exactly as a person would type them.

        The password is written to the pty, so it never appears in an argument
        or in the process list.
        """
        master, slave = pty.openpty()
        out = b""
        transcript = []
        proc = None
        try:
            try:
                proc = subprocess.Popen(
                    ["sftp", *self._base_args(), self.target],
                    stdin=slave, stdout=slave, stderr=slave, close_fds=True,
                    preexec_fn=self._own_the_tty)
            except FileNotFoundError:
                raise TransferError("the `sftp` command is not installed")
            os.close(slave)
            slave = None

            answered = sent = False
            deadline = time.monotonic() + (timeout or 86400)
            while True:
                ready, _, _ = select.select([master], [], [], 0.5)
                if ready:
                    try:
                        chunk = os.read(master, 4096)
                    except OSError:
                        break                    # pty hung up: child is done
                    if not chunk:
                        break
                    out += chunk
                    tail = re.split(rb"[\r\n]", out)[-1].strip()

                    if not answered and (self._PROMPT.search(tail)
                                         or self._PROMPT_ANY.search(chunk)):
                        os.write(master, self.password.encode() + b"\n")
                        answered = True
                        transcript.append("<password sent>")
                    elif not sent and b"sftp>" in out:
                        # Authenticated: it is showing its own prompt.
                        os.write(master, (script.rstrip("\n") + "\nquit\n").encode())
                        sent = True
                elif proc.poll() is not None:
                    break
                if abort is not None and abort.is_set():
                    proc.kill()
                    raise TransferError(getattr(abort, "reason", "") or "cancelled")
                if time.monotonic() > deadline:
                    proc.kill()
                    if not answered:
                        raise TransferError(
                            f"no password prompt appeared from {self.target} "
                            "within the timeout — the server may be offering "
                            "only key auth, or the connection stalled")
                    raise TransferError("sftp timed out after sending the password")
            proc.wait()
        finally:
            if slave is not None:
                os.close(slave)
            try:
                os.close(master)
            except OSError:
                pass
            if proc and proc.poll() is None:
                proc.kill()
            if proc:
                proc.wait()

        text = out.decode("utf-8", "replace")
        self.transcript = transcript + [ln for ln in text.splitlines() if ln.strip()]
        # Drop the prompt itself and the terminal's echo of our commands.
        keep = [ln for ln in text.splitlines()
                if not self._PROMPT.search(ln.strip().encode())]
        joined = "\n".join(keep)
        if "Permission denied" in joined or "Authentication failed" in joined:
            raise TransferError(f"authentication failed for {self.target}")
        return joined

    def _run(self, commands, timeout=120, tolerant=True, abort=None):
        """
        Run a batch of sftp commands, returning its output.

        `abort` is an Event: once set, the sftp process is killed and this
        raises, so a cancelled or stalled transfer really stops rather than
        carrying on in the background after its worker has moved on.
        """
        if tolerant:
            # A leading '-' tells sftp to keep going past a failing command,
            # so one missing file cannot abort the whole batch.
            commands = [c if c.startswith("-") else "-" + c for c in commands]
        script = "\n".join(commands) + "\n"
        if self.password:
            return self._run_with_password(script, timeout, abort)
        try:
            proc = subprocess.Popen(
                ["sftp", "-b", "-", *self._base_args(), self.target],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True,
            )
        except FileNotFoundError:
            raise TransferError("the `sftp` command is not installed")
        deadline = None if timeout is None else time.monotonic() + timeout
        pending = script
        try:
            while True:
                try:
                    # Retrying communicate() after a timeout loses no output.
                    # The input goes on the first call only (newer Pythons
                    # refuse it on a retry).
                    stdout, stderr = proc.communicate(pending, timeout=0.25)
                    break
                except subprocess.TimeoutExpired:
                    pending = None
                    if abort is not None and abort.is_set():
                        raise TransferError(getattr(abort, "reason", "") or "cancelled")
                    if deadline is not None and time.monotonic() > deadline:
                        raise TransferError(f"sftp timed out after {timeout}s")
        finally:
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.communicate(timeout=5)
                except (subprocess.TimeoutExpired, ValueError, OSError):
                    pass
        # sftp reports per-command failures ("Can't ls", "remote open ...
        # failed") on stderr while the batch itself still exits 0, so the
        # two streams have to be read together or errors vanish silently.
        out = (stdout or "") + (stderr or "")
        if proc.returncode != 0 and not stdout:
            raise TransferError(self._explain(out))
        return out

    def _explain(self, out):
        """Translate OpenSSH's terser failures into something actionable."""
        low = out.lower()
        if "remote host identification has changed" in low:
            return (f"the host key for {self.host} has CHANGED since last time. "
                    "This is refused. If the server was genuinely rebuilt, remove "
                    f"its entry with: ssh-keygen -R {self.host}")
        if "host key verification failed" in low:
            return f"could not verify the host key for {self.host}"
        if "permission denied" in low:
            return (f"authentication failed for {self.target} — "
                    "set the site's Auth field to 'password' if it wants one")
        if "connection refused" in low:
            return f"{self.host} refused the connection (is SSH running?)"
        if "no route to host" in low or "could not resolve" in low:
            return f"cannot reach {self.host}"
        msg = [l for l in out.strip().splitlines() if l.strip()]
        return msg[-1] if msg else "sftp failed"

    def connect(self):
        out = self._run(["pwd"], timeout=30, tolerant=False)
        for line in out.splitlines():
            if line.startswith("Remote working directory:"):
                self._home = line.split(":", 1)[1].strip()
        if not self._home:
            raise TransferError(f"could not open an SFTP session to {self.host}")

    def close(self):
        if self._ctl_dir and os.path.isdir(self._ctl_dir):
            try:
                subprocess.run(
                    ["ssh", "-O", "exit", "-o", f"ControlPath={self._ctl}", self.target],
                    capture_output=True, timeout=10,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
            shutil.rmtree(self._ctl_dir, ignore_errors=True)

    def home(self):
        return self.start or self._home or "/"

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

        The same reading drives the stall watchdog: once the size has been
        readable at least once, `stall_seconds` without it growing kills the
        transfer. If `progress` raises (the queue's way of saying "cancel"),
        the sftp process is killed before the exception goes any further.
        """
        result = {}
        abort = threading.Event()
        abort.reason = ""

        def run():
            try:
                result["out"] = self._run([command], timeout=None, abort=abort)
            except Exception as e:                      # noqa: BLE001
                result["err"] = e

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        started = last_moved = time.monotonic()
        done, readable = 0, False
        try:
            while worker.is_alive():
                worker.join(0.5)
                if not worker.is_alive():
                    break
                now = time.monotonic()
                seen = -1
                if watch:
                    try:
                        seen = watch()
                    except Exception:                   # noqa: BLE001
                        seen = -1
                if seen >= 0:
                    readable = True
                    if seen > done:
                        done, last_moved = seen, now
                if (self.stall_seconds and readable
                        and now - last_moved > self.stall_seconds):
                    abort.reason = (f"{verb} stalled: no progress for "
                                    f"{int(now - last_moved)}s")
                    abort.set()
                    break
                if progress:
                    elapsed = now - started
                    progress(done, total, done / elapsed if elapsed > 0.5 else 0)
        except BaseException:
            abort.reason = abort.reason or "cancelled"
            abort.set()
            worker.join(15)
            raise
        if abort.is_set():
            worker.join(15)
            raise TransferError(abort.reason)

        if "err" in result:
            raise result["err"]
        self._check(result.get("out", ""), verb)
        if progress and total > 0:
            progress(total, total, 0)

    def _remote_size(self, path):
        """One cheap stat over the shared SSH connection, -1 if unavailable."""
        if self.password:
            # A password-auth or chrooted account usually has no shell, and a
            # second ssh here would sit on a prompt. Fall back to sftp's ls.
            return self.size_of(path)
        try:
            r = subprocess.run(
                ["ssh", *self._base_args(for_ssh=True), self.target,
                 f"stat -c %s -- {shlex.quote(path)} 2>/dev/null || echo -1"],
                capture_output=True, text=True, timeout=20,
            )
            return int((r.stdout or "-1").strip().split()[-1])
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
            return -1

    def read_to(self, path, local_path, progress=None):
        verb = "get"
        total = self.size_of(path)

        def watch():
            try:
                return os.path.getsize(local_path)
            except OSError:
                return 0

        self._transfer(f"-{verb} {self.q(path)} {self.q(local_path)}",
                       verb, total, watch, progress)

    def write_from(self, local_path, path, progress=None):
        verb = "put"
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

        Requires a shell, so it is skipped entirely for password/chrooted
        accounts (`internal-sftp` has no shell to run sha256sum in).

        Needs a shell on the remote, which a locked-down sftp-only account
        will not have; returning None there simply falls back to a size
        comparison rather than failing the transfer.
        """
        if self.password:
            return None
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
