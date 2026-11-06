"""S3 and S3-compatible stores, SigV4 signed by hand over urllib."""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from .base import Backend, Entry, TransferError, _int, _pump


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
