"""Throwaway WebDAV + S3 servers, written straight from the specs, used only
to exercise foxzilla's client code. Independent of foxzilla entirely."""
import base64, hashlib, hmac, os, shutil, threading, urllib.parse, datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------- WebDAV ----------------
class DAV(BaseHTTPRequestHandler):
    root = None
    def log_message(self, *a): pass
    def _fs(self):
        p = urllib.parse.unquote(self.path.split("?")[0]).lstrip("/")
        return os.path.join(self.root, p)
    def _auth_ok(self):
        h = self.headers.get("Authorization", "")
        if not h.startswith("Basic "): return False
        u, _, pw = base64.b64decode(h[6:]).decode().partition(":")
        return (u, pw) == ("dave", "hunter2")
    def _guard(self):
        if not self._auth_ok():
            self.send_response(401); self.send_header("Content-Length","0"); self.end_headers()
            return False
        return True
    def do_PROPFIND(self):
        if not self._guard(): return
        fs = self._fs()
        if not os.path.isdir(fs):
            self.send_response(404); self.send_header("Content-Length","0"); self.end_headers(); return
        base = urllib.parse.quote(self.path.split("?")[0])
        if not base.endswith("/"): base += "/"
        items = [(base, True, 0, os.path.getmtime(fs))]
        for n in sorted(os.listdir(fs)):
            f = os.path.join(fs, n); d = os.path.isdir(f)
            items.append((base + urllib.parse.quote(n) + ("/" if d else ""),
                          d, 0 if d else os.path.getsize(f), os.path.getmtime(f)))
        parts = ['<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">']
        for href, d, size, mt in items:
            stamp = dt.datetime.fromtimestamp(mt, dt.timezone.utc).strftime(
                "%a, %d %b %Y %H:%M:%S GMT")
            rt = "<d:collection/>" if d else ""
            parts.append(f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>"
                         f"<d:resourcetype>{rt}</d:resourcetype>"
                         f"<d:getcontentlength>{size}</d:getcontentlength>"
                         f"<d:getlastmodified>{stamp}</d:getlastmodified>"
                         f"</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")
        parts.append("</d:multistatus>")
        body = "".join(parts).encode()
        self.send_response(207); self.send_header("Content-Type","application/xml")
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        if not self._guard(): return
        fs = self._fs()
        if not os.path.isfile(fs):
            self.send_response(404); self.send_header("Content-Length","0"); self.end_headers(); return
        data = open(fs,"rb").read()
        self.send_response(200); self.send_header("Content-Length", str(len(data))); self.end_headers()
        self.wfile.write(data)
    def do_PUT(self):
        if not self._guard(): return
        n = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(n)
        fs = self._fs()
        if not os.path.isdir(os.path.dirname(fs)):
            self.send_response(409); self.send_header("Content-Length","0"); self.end_headers(); return
        open(fs,"wb").write(data)
        self.send_response(201); self.send_header("Content-Length","0"); self.end_headers()
    def do_MKCOL(self):
        if not self._guard(): return
        try: os.mkdir(self._fs()); code = 201
        except OSError: code = 409
        self.send_response(code); self.send_header("Content-Length","0"); self.end_headers()
    def do_DELETE(self):
        if not self._guard(): return
        fs = self._fs()
        if os.path.isdir(fs): shutil.rmtree(fs); code = 204
        elif os.path.isfile(fs): os.remove(fs); code = 204
        else: code = 404
        self.send_response(code); self.send_header("Content-Length","0"); self.end_headers()
    def do_MOVE(self):
        if not self._guard(): return
        dest = urllib.parse.unquote(urllib.parse.urlparse(self.headers["Destination"]).path)
        target = os.path.join(self.root, dest.lstrip("/"))
        try: os.replace(self._fs(), target); code = 201
        except OSError: code = 409
        self.send_response(code); self.send_header("Content-Length","0"); self.end_headers()

# ---------------- S3 ----------------
AK, SK, REGION, BUCKET = "AKIATESTTESTTEST0000", "sekrit/key+with/slashes", "eu-west-1", "testbucket"

def sigv4(method, path, query, headers, payload_hash, amzdate, datestamp):
    """Reference implementation, written from the SigV4 spec, not shared
    with the client - a mismatch means one of the two got it wrong."""
    signed = sorted(k.lower() for k in headers)
    ch = "".join(f"{k}:{headers[k].strip()}\n" for k in signed)
    sh = ";".join(signed)
    cq = "&".join(f"{urllib.parse.quote(k,safe='')}={urllib.parse.quote(v,safe='')}"
                  for k, v in sorted(query.items()))
    creq = "\n".join([method, path, cq, ch, sh, payload_hash])
    scope = f"{datestamp}/{REGION}/s3/aws4_request"
    sts = "\n".join(["AWS4-HMAC-SHA256", amzdate, scope,
                     hashlib.sha256(creq.encode()).hexdigest()])
    k = ("AWS4"+SK).encode()
    for part in (datestamp, REGION, "s3", "aws4_request"):
        k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    return hmac.new(k, sts.encode(), hashlib.sha256).hexdigest(), sh, scope

class S3(BaseHTTPRequestHandler):
    root = None
    bad_sigs = []
    def log_message(self, *a): pass
    def _verify(self, body):
        auth = self.headers.get("Authorization","")
        try:
            cred = auth.split("Credential=")[1].split(",")[0]
            sh   = auth.split("SignedHeaders=")[1].split(",")[0].strip()
            sig  = auth.split("Signature=")[1].strip()
        except IndexError:
            self.bad_sigs.append(("malformed", auth)); return False
        amzdate = self.headers["x-amz-date"]; datestamp = amzdate[:8]
        hdrs = {k: self.headers[k] for k in sh.split(";")}
        parsed = urllib.parse.urlparse(self.path)
        query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
        ph = self.headers.get("x-amz-content-sha256") or hashlib.sha256(body).hexdigest()
        if ph != hashlib.sha256(body).hexdigest():
            self.bad_sigs.append(("payload-hash", ph)); return False
        want, want_sh, scope = sigv4(self.command, urllib.parse.quote(
            urllib.parse.unquote(parsed.path), safe="/~"), query, hdrs, ph, amzdate, datestamp)
        if not cred.startswith(AK) or cred.split("/",1)[1] != scope:
            self.bad_sigs.append(("scope", cred)); return False
        if want != sig:
            self.bad_sigs.append(("signature", f"want {want[:16]} got {sig[:16]}")); return False
        return True
    def _send(self, code, body=b"", ctype="application/xml"):
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        if self.command != "HEAD": self.wfile.write(body)
    def _err(self, code, msg):
        self._send(code, f"<Error><Message>{msg}</Message></Error>".encode())
    def _key(self):
        p = urllib.parse.unquote(urllib.parse.urlparse(self.path).path).lstrip("/")
        if not p.startswith(BUCKET): return None
        return p[len(BUCKET):].lstrip("/")
    def _path(self, key): return os.path.join(self.root, key.replace("/", "%2F"))
    def do_GET(self):
        body = b""
        if not self._verify(body): return self._err(403, "SignatureDoesNotMatch")
        parsed = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(parsed.query))
        key = self._key()
        if key is None: return self._err(404, "NoSuchBucket")
        if q.get("list-type") == "2":
            prefix, delim = q.get("prefix",""), q.get("delimiter","")
            keys = sorted(urllib.parse.unquote(f).replace("%2F","/")
                          for f in os.listdir(self.root))
            common, contents = set(), []
            for k in keys:
                if not k.startswith(prefix): continue
                rest = k[len(prefix):]
                if delim and delim in rest:
                    common.add(prefix + rest.split(delim)[0] + delim)
                else:
                    contents.append(k)
            NS = "http://s3.amazonaws.com/doc/2006-03-01/"
            x = [f'<?xml version="1.0"?><ListBucketResult xmlns="{NS}">',
                 "<IsTruncated>false</IsTruncated>"]
            for c in sorted(common):
                x.append(f"<CommonPrefixes><Prefix>{c}</Prefix></CommonPrefixes>")
            for k in contents:
                st = os.stat(self._path(k))
                ts = dt.datetime.fromtimestamp(st.st_mtime, dt.timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z")
                x.append(f"<Contents><Key>{k}</Key><Size>{st.st_size}</Size>"
                         f"<LastModified>{ts}</LastModified></Contents>")
            x.append("</ListBucketResult>")
            return self._send(200, "".join(x).encode())
        fp = self._path(key)
        if not os.path.isfile(fp): return self._err(404, "NoSuchKey")
        self._send(200, open(fp,"rb").read(), "application/octet-stream")
    def do_PUT(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        if not self._verify(body): return self._err(403, "SignatureDoesNotMatch")
        key = self._key()
        src = self.headers.get("x-amz-copy-source")
        if src:
            sk = urllib.parse.unquote(src).lstrip("/")[len(BUCKET):].lstrip("/")
            if not os.path.isfile(self._path(sk)): return self._err(404, "NoSuchKey")
            body = open(self._path(sk),"rb").read()
        open(self._path(key),"wb").write(body)
        self._send(200)
    def do_DELETE(self):
        if not self._verify(b""): return self._err(403, "SignatureDoesNotMatch")
        fp = self._path(self._key())
        if os.path.isfile(fp): os.remove(fp); return self._send(204)
        self._err(404, "NoSuchKey")

def serve(handler, root):
    handler.root = root
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]
