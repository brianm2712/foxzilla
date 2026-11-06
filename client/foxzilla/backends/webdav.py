"""WebDAV, via urllib and ElementTree."""
from __future__ import annotations

import base64
import datetime as dt
import os
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from .base import Backend, Entry, TransferError, _int, _pump


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
