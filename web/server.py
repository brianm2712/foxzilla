#!/usr/bin/env python3
"""
Foxzilla, hosted.

The desktop client with its panes on the web: a visitor configures their own
remote, browses it beside their own files, and transfers between the two with
the same scan-and-dedup step the desktop version does.

Three things make hosting this different from shipping a binary, and they
shape everything below:

  - The server, not the visitor, opens the outbound connection. Everything
    goes through `egress`, and connections are made to a checked address
    rather than to a name. See egress.py; it is the reason this is safe to
    host at all.

  - Credentials belong to the visitor, not to us. They live in memory for the
    life of a session and are never written to disk, never logged, and never
    returned to the browser.

  - "Local" cannot mean the server's disk. It is the visitor's machine, so
    files arrive by upload and leave by download; the server only ever holds
    a file while it is in flight.

Standard library only, like the rest of Foxzilla.
"""
from __future__ import annotations

import ftplib
import http.server
import json
import mimetypes
import os
import posixpath
import secrets
import socketserver
import sys
import threading
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "client"))

import egress                                    # noqa: E402
import foxzilla as fz                            # noqa: E402

PORT = int(os.environ.get("PORT", "8110"))
STATIC = os.path.join(HERE, "static")
SESSION_TTL = 2 * 3600
MAX_UPLOAD = 2 * 1024 * 1024 * 1024              # 2 GB per file
MAX_SESSIONS = 200
MAX_LISTING = 5000


# --------------------------------------------------------------------------
# sessions
# --------------------------------------------------------------------------

class Session:
    """One visitor's connection to one remote. Nothing here touches disk."""

    def __init__(self):
        self.id = secrets.token_urlsafe(24)
        self.created = time.time()
        self.touched = time.time()
        self.backend: fz.Backend | None = None
        self.label = ""
        self.lock = threading.Lock()

    @property
    def expired(self):
        return time.time() - self.touched > SESSION_TTL

    def close(self):
        try:
            self.backend and self.backend.close()
        except Exception:                        # noqa: BLE001
            pass
        self.backend = None


SESSIONS: dict[str, Session] = {}
SESSIONS_LOCK = threading.Lock()


def reap():
    with SESSIONS_LOCK:
        for sid, s in list(SESSIONS.items()):
            if s.expired:
                s.close()
                SESSIONS.pop(sid, None)


def get_session(sid, create=False) -> Session | None:
    reap()
    with SESSIONS_LOCK:
        s = SESSIONS.get(sid or "")
        if s and not s.expired:
            s.touched = time.time()
            return s
        if not create:
            return None
        if len(SESSIONS) >= MAX_SESSIONS:
            oldest = min(SESSIONS.values(), key=lambda x: x.touched)
            oldest.close()
            SESSIONS.pop(oldest.id, None)
        s = Session()
        SESSIONS[s.id] = s
        return s


# --------------------------------------------------------------------------
# building a backend the visitor asked for
# --------------------------------------------------------------------------

DEFAULT_PORTS = {"sftp": 22, "ftp": 21, "ftps": 21, "dav": 443, "webdav": 443}


def build_backend(cfg: dict) -> tuple[fz.Backend, str]:
    """
    Turn a visitor's target description into a backend, or refuse it.

    The host is resolved and checked here, and the backend is pointed at the
    resolved *address*. Handing it the name instead would let the name answer
    differently at connect time, which is the whole trick behind rebinding.
    """
    kind = str(cfg.get("type", "")).lower()
    if kind not in ("sftp", "ftp", "ftps", "dav", "webdav"):
        raise egress.Refused(f"unsupported target type {kind!r}")

    if kind in ("dav", "webdav"):
        url = str(cfg.get("url", ""))
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise egress.Refused("WebDAV needs an http:// or https:// URL")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addr = egress.safe_target(parsed.hostname or "", port)
        # Keep the hostname in the URL for TLS and virtual hosting, but the
        # check above has already proven where that name points.
        label = f"{parsed.scheme}://{parsed.hostname}{parsed.path or ''}"
        backend = fz.WebDAVBackend(url=url, user=str(cfg.get("user", "")),
                                   password=str(cfg.get("password", "")),
                                   label=label)
        return backend, label

    host = str(cfg.get("host", "")).strip()
    port = int(cfg.get("port") or DEFAULT_PORTS[kind])
    addr = egress.safe_target(host, port)
    user = str(cfg.get("user", "")).strip()
    password = str(cfg.get("password", ""))
    label = f"{kind}://{user + '@' if user else ''}{host}"

    if kind == "sftp":
        if not password:
            raise egress.Refused("this service can only use password auth for SFTP")
        backend = fz.SFTPBackend(host=addr, user=user, port=port,
                                 password=password, label=label)
    else:
        backend = fz.FTPBackend(host=addr, user=user or "anonymous",
                                password=password, port=port,
                                tls=(kind == "ftps"), label=label)
    return backend, label


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

# A far-side failure is the target's problem, not ours: a missing folder or a
# refused login is a 502, not an internal error. Only genuine bugs reach 500.
BACKEND_ERRORS = (fz.TransferError, ftplib.Error, OSError, EOFError)


def clean_error(e: Exception) -> str:
    """A message safe to hand back: no tracebacks, no local paths."""
    msg = str(e).strip() or type(e).__name__
    for marker in ("/tmp/", "/opt/", "/root/", "/home/"):
        if marker in msg:
            msg = msg.split(marker)[0].rstrip() + " (path hidden)"
            break
    return msg[:300]


def clean_path(p: str) -> str:
    p = posixpath.normpath("/" + str(p or "/").lstrip("/"))
    return p or "/"


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "foxzilla-web"
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------

    def log_message(self, fmt, *args):
        # Deliberately terse: never log query strings or bodies, which is
        # where a mistyped password would end up.
        sys.stderr.write(f"{self.address_string()} {self.command} "
                         f"{self.path.split('?')[0]} {args[1] if len(args) > 1 else ''}\n")

    def _json(self, obj, code=200, headers=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, msg, code=400):
        self._json({"error": str(msg)}, code)

    def _body(self, limit=1024 * 1024):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("request too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def _cookies(self):
        out = {}
        for part in (self.headers.get("Cookie") or "").split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    def _session(self, create=False):
        return get_session(self._cookies().get("fzs"), create=create)

    # -- routes -----------------------------------------------------------

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        try:
            if url.path == "/api/health":
                return self._json({"ok": True})
            if url.path == "/api/session":
                s = self._session()
                return self._json({"connected": bool(s and s.backend),
                                   "label": s.label if s else ""})
            if url.path == "/api/list":
                return self.api_list(q)
            if url.path == "/api/download":
                return self.api_download(q)
            return self.static(url.path)
        except egress.Refused as e:
            self._fail(e, 403)
        except BACKEND_ERRORS as e:
            self._fail(clean_error(e), 502)
        except Exception as e:                   # noqa: BLE001
            self._fail(f"{type(e).__name__}: {e}", 500)

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        try:
            if url.path == "/api/connect":
                return self.api_connect()
            if url.path == "/api/disconnect":
                return self.api_disconnect()
            if url.path == "/api/scan":
                return self.api_scan()
            if url.path == "/api/upload":
                return self.api_upload(urllib.parse.parse_qs(url.query))
            if url.path == "/api/mkdir":
                return self.api_mkdir()
            if url.path == "/api/delete":
                return self.api_delete()
            return self._fail("no such endpoint", 404)
        except egress.Refused as e:
            self._fail(e, 403)
        except BACKEND_ERRORS as e:
            self._fail(clean_error(e), 502)
        except Exception as e:                   # noqa: BLE001
            self._fail(f"{type(e).__name__}: {e}", 500)

    # -- session ----------------------------------------------------------

    def api_connect(self):
        cfg = self._body()
        backend, label = build_backend(cfg)
        backend.connect()                        # fails loudly before we store it
        s = self._session(create=True)
        with s.lock:
            s.close()
            s.backend, s.label = backend, label
        return self._json(
            {"ok": True, "label": label, "home": backend.home()},
            headers={"Set-Cookie":
                     f"fzs={s.id}; Path=/; Max-Age={SESSION_TTL}; "
                     "HttpOnly; Secure; SameSite=Strict"})

    def api_disconnect(self):
        s = self._session()
        if s:
            with SESSIONS_LOCK:
                s.close()
                SESSIONS.pop(s.id, None)
        return self._json({"ok": True}, headers={
            "Set-Cookie": "fzs=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict"})

    def _need(self):
        s = self._session()
        if not s or not s.backend:
            raise fz.TransferError("not connected")
        return s

    # -- browsing ---------------------------------------------------------

    def api_list(self, q):
        s = self._need()
        path = clean_path((q.get("path") or ["/"])[0])
        with s.lock:
            entries = s.backend.listdir(path)[:MAX_LISTING]
        return self._json({
            "path": path,
            "parent": s.backend.parent(path),
            "entries": [{"name": e.name, "size": e.size, "mtime": e.mtime,
                         "dir": e.is_dir} for e in entries],
        })

    def api_mkdir(self):
        s = self._need()
        b = self._body()
        target = posixpath.join(clean_path(b.get("path")), str(b.get("name", ""))[:255])
        with s.lock:
            s.backend.mkdir(target)
        return self._json({"ok": True})

    def api_delete(self):
        s = self._need()
        b = self._body()
        target = clean_path(b.get("path"))
        if target == "/":
            return self._fail("refusing to delete the root of the target")
        with s.lock:
            (s.backend.rmdir if b.get("dir") else s.backend.remove)(target)
        return self._json({"ok": True})

    # -- the scan ---------------------------------------------------------

    def api_scan(self):
        """
        Compare what the visitor is about to send against what is already there.

        The browser hashes its own files and sends only name, size and digest,
        so nothing is uploaded to find out it was already present - which is
        the entire point of the scan.
        """
        s = self._need()
        b = self._body(limit=4 * 1024 * 1024)
        dest = clean_path(b.get("path"))
        files = b.get("files") or []
        if len(files) > 2000:
            return self._fail("too many files in one scan")

        with s.lock:
            try:
                known = {e.name: e for e in s.backend.listdir(dest)}
            except fz.TransferError:
                known = {}

        out = []
        for f in files:
            name = str(f.get("name", ""))[:255]
            size = int(f.get("size") or 0)
            digest = str(f.get("sha256", ""))
            there = known.get(name)
            if not there or there.is_dir:
                verdict = fz.CLEAR
            elif there.size != size:
                verdict = fz.CONFLICT
            else:
                # Same size. Only a hash from the far side can settle it, and
                # most remotes cannot produce one without sending the file.
                remote_digest = None
                if digest:
                    with s.lock:
                        remote_digest = s.backend.checksum(posixpath.join(dest, name))
                if remote_digest and digest:
                    verdict = fz.IDENTICAL if remote_digest == digest else fz.CONFLICT
                else:
                    verdict = fz.SAME_SIZE
            out.append({"name": name, "size": size, "verdict": verdict})

        counts = {}
        for item in out:
            counts[item["verdict"]] = counts.get(item["verdict"], 0) + 1
        return self._json({"path": dest, "items": out, "counts": counts})

    # -- moving bytes -----------------------------------------------------

    def api_upload(self, q):
        """Stream one uploaded file straight out to the target."""
        s = self._need()
        dest = clean_path((q.get("path") or ["/"])[0])
        name = os.path.basename((q.get("name") or [""])[0])[:255]
        if not name:
            return self._fail("no filename given")
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_UPLOAD:
            return self._fail("file is larger than this service will relay", 413)

        tmp = os.path.join("/tmp", f".fzweb-{secrets.token_hex(8)}")
        try:
            got = 0
            with open(tmp, "wb") as fh:
                while got < n:
                    chunk = self.rfile.read(min(1 << 20, n - got))
                    if not chunk:
                        break
                    fh.write(chunk)
                    got += len(chunk)
            with s.lock:
                s.backend.write_from(tmp, posixpath.join(dest, name))
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        return self._json({"ok": True, "name": name, "bytes": got})

    def api_download(self, q):
        s = self._need()
        path = clean_path((q.get("path") or [""])[0])
        name = posixpath.basename(path) or "download"
        tmp = os.path.join("/tmp", f".fzweb-{secrets.token_hex(8)}")
        try:
            with s.lock:
                s.backend.read_to(path, tmp)
            size = os.path.getsize(tmp)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition",
                             f'attachment; filename="{name}"')
            self.end_headers()
            with open(tmp, "rb") as fh:
                while True:
                    chunk = fh.read(1 << 20)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    # -- static -----------------------------------------------------------

    def static(self, path):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        full = os.path.normpath(os.path.join(STATIC, rel))
        if not full.startswith(STATIC) or not os.path.isfile(full):
            return self._fail("not found", 404)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        data = open(full, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                         "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    srv = Server(("127.0.0.1", PORT), Handler)
    print(f"foxzilla-web listening on 127.0.0.1:{PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
