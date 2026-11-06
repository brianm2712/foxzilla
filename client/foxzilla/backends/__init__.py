"""Every place files can live, and the factory that builds one from a site."""
from __future__ import annotations

import os

from .base import (CHUNK, Backend, Entry, TransferError, _int, _ls_time, _pump,
                   clean_typed_path, safe_name, under_root)
from .ftp import FTPBackend, _mlsd_time
from .local import LocalBackend
from .s3 import S3Backend, _iso_time, _s3_quote
from .sftp import SFTPBackend
from .webdav import WebDAVBackend, _http_time


def _flag(value):
    """A site setting as a bool: JSON true, or 'yes'/'true'/'1' typed in the dialog."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "yes", "true", "on", "y")
    return bool(value)


def make_backend(site, password=""):
    """Build the backend a site describes, ready to connect."""
    kind = site.get("type", "local")
    name = site.get("name")

    if kind == "local":
        backend = LocalBackend(label=name or "Local", start=site.get("path"))
    elif kind == "sftp":
        backend = SFTPBackend(
            host=site["host"], user=site.get("user"), port=site.get("port"),
            identity=site.get("identity"), label=name,
            password=password if site.get("auth") == "password" else "",
            start=site.get("path"),
        )
    elif kind in ("ftp", "ftps"):
        backend = FTPBackend(
            host=site["host"], user=site.get("user") or "anonymous",
            password=password, port=site.get("port", 21),
            tls=(kind == "ftps" or bool(site.get("tls"))),
            passive=site.get("passive", True), label=name,
            ca_file=site.get("ca_file") or None,
            insecure_tls=_flag(site.get("insecure_tls")),
        )
    elif kind == "s3":
        backend = S3Backend(
            bucket=site["bucket"],
            access_key=site.get("access_key") or os.environ.get("AWS_ACCESS_KEY_ID", ""),
            secret_key=site.get("secret_key") or os.environ.get("AWS_SECRET_ACCESS_KEY", ""),
            region=site.get("region", "us-east-1"),
            endpoint=site.get("endpoint"),
            session_token=site.get("session_token") or os.environ.get("AWS_SESSION_TOKEN"),
            label=name,
        )
    elif kind in ("dav", "webdav"):
        backend = WebDAVBackend(url=site["url"], user=site.get("user", ""),
                                password=password, label=name)
    else:
        raise TransferError(f"unknown site type: {kind!r}")

    # Sites that feed a watcher get a completion marker written once a
    # transfer here is finished and verified.
    backend.signal_complete = bool(site.get("drop"))
    return backend
