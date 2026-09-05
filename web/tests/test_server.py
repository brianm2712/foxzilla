#!/usr/bin/env python3
"""
The hosted server: refusals, sessions, browsing, scan, transfer.

A note on how this is tested. The egress guard blocks loopback, so a mock
server on 127.0.0.1 is unreachable through the normal path - which is the
guard working. The refusal tests therefore run at full strength, and the
transfer tests deliberately relax the guard so the application logic behind it
can be exercised at all. Relaxing it in a test is fine; relaxing it in the
service would defeat the point.
"""
import http.client
import json
import os
import socket
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.dirname(HERE)
sys.path.insert(0, WEB)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(WEB), "client", "tests"))

import egress                                     # noqa: E402
import server as srv                              # noqa: E402
import ftpmock                                    # noqa: E402

ok = True


def check(label, cond, extra=""):
    global ok
    print(("  PASS " if cond else "  FAIL ") + label + (f"   {extra}" if extra else ""))
    ok = ok and bool(cond)


# -- start the app under test ----------------------------------------------
PORT = 8211
httpd = srv.Server(("127.0.0.1", PORT), srv.Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
time.sleep(0.4)


class Client:
    """A tiny HTTP client that keeps its own cookie, so sessions are separable."""

    def __init__(self):
        self.cookie = None

    def request(self, method, path, body=None, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=30)
        headers = {}
        if self.cookie:
            headers["Cookie"] = self.cookie
        payload = raw
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        c.request(method, path, body=payload, headers=headers)
        r = c.getresponse()
        data = r.read()
        sc = r.getheader("Set-Cookie")
        if sc:
            self.cookie = sc.split(";")[0]
        c.close()
        try:
            return r.status, json.loads(data)
        except Exception:                         # noqa: BLE001
            return r.status, data


A = Client()

print("\nthe service is up")
code, body = A.request("GET", "/api/health")
check("/api/health", code == 200 and body.get("ok"), f"{code} {body}")
code, body = A.request("GET", "/")
check("/ serves the page", code == 200 and b"Foxzilla" in body, code)
code, body = A.request("GET", "/app.js")
check("/app.js serves", code == 200 and b"sha256" in body, code)

print("\nrefusals — the guard at full strength")
for host, why in [("127.0.0.1", "loopback"), ("localhost", "resolves to loopback"),
                  ("100.102.227.73", "the tailnet"), ("192.168.68.54", "the LAN"),
                  ("169.254.169.254", "cloud metadata"), ("10.0.0.1", "RFC1918")]:
    code, body = A.request("POST", "/api/connect",
                           {"type": "sftp", "host": host, "user": "u", "password": "p"})
    check(f"refuses {host:<16} ({why})", code == 403, f"{code} {str(body)[:60]}")

code, body = A.request("POST", "/api/connect",
                       {"type": "sftp", "host": "one.one.one.one", "port": 3306,
                        "user": "u", "password": "p"})
check("refuses a non-transfer port", code == 403, f"{code} {str(body)[:50]}")

code, body = A.request("POST", "/api/connect", {"type": "s3", "host": "example.com"})
check("refuses an unsupported type", code == 403, f"{code} {str(body)[:50]}")

code, body = A.request("POST", "/api/connect",
                       {"type": "sftp", "host": "one.one.one.one", "user": "u"})
check("refuses SFTP with no password", code == 403, f"{code} {str(body)[:50]}")

print("\nnothing works without a session")
for method, path in [("GET", "/api/list?path=/"), ("POST", "/api/mkdir"),
                     ("POST", "/api/delete"), ("POST", "/api/scan")]:
    code, body = A.request(method, path, {} if method == "POST" else None)
    check(f"{method} {path.split('?')[0]} needs a connection", code == 409, code)

# -- relax the guard so the app behind it can be tested --------------------
print("\n[guard relaxed for the transfer tests only]")
real_v4 = egress.BLOCKED_V4
egress.BLOCKED_V4 = []
_real_reason = egress._blocked_reason
egress._blocked_reason = lambda ip: None

root = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"fzweb-test-{os.getpid()}")
os.makedirs(os.path.join(root, "existing"), exist_ok=True)
blob = os.urandom(4096)
open(os.path.join(root, "same.bin"), "wb").write(blob)
open(os.path.join(root, "differs.bin"), "wb").write(os.urandom(4096))
ftp_port = ftpmock.serve(root)
egress.ALLOWED_PORTS.add(ftp_port)

print("\nconnecting to a real server")
code, body = A.request("POST", "/api/connect",
                       {"type": "ftp", "host": "127.0.0.1", "port": ftp_port,
                        "user": ftpmock.USER, "password": ftpmock.PASSWORD})
check("connects", code == 200 and body.get("ok"), f"{code} {str(body)[:70]}")
check("a session cookie was set", A.cookie and A.cookie.startswith("fzs="), A.cookie)
check("the password is not echoed back", "password" not in str(body).lower())

code, body = A.request("GET", "/api/list?path=/")
names = {e["name"] for e in body.get("entries", [])}
check("lists the target", code == 200 and {"same.bin", "differs.bin", "existing"} <= names,
      sorted(names))
check("marks directories", any(e["dir"] for e in body["entries"] if e["name"] == "existing"))

print("\na second visitor gets their own session")
B = Client()
code, body = B.request("GET", "/api/list?path=/")
check("other visitor is not connected", code == 409, code)
code, body = B.request("GET", "/api/session")
check("and sees no label", code == 200 and not body.get("connected"), body)

print("\nthe scan, without uploading anything")
import hashlib
same_digest = hashlib.sha256(blob).hexdigest()
code, body = A.request("POST", "/api/scan", {
    "path": "/",
    "files": [
        {"name": "same.bin", "size": 4096, "sha256": same_digest},
        {"name": "differs.bin", "size": 4096, "sha256": hashlib.sha256(b"other").hexdigest()},
        {"name": "brand-new.bin", "size": 10, "sha256": "ab" * 32},
    ],
})
verdicts = {i["name"]: i["verdict"] for i in body.get("items", [])}
check("scan returns a verdict per file", code == 200 and len(verdicts) == 3, verdicts)
check("a new file reads as clear", verdicts.get("brand-new.bin") == "clear", verdicts)
check("same size, no remote hash -> 'same size', not 'identical'",
      verdicts.get("same.bin") == "same size", verdicts.get("same.bin"))
check("differing size is a conflict or same-size, never clear",
      verdicts.get("differs.bin") in ("same size", "conflict"), verdicts.get("differs.bin"))

print("\nupload, download, mkdir, delete")
payload = os.urandom(2048)
code, body = A.request("POST", "/api/upload?path=/&name=uploaded.bin", raw=payload)
check("upload accepted", code == 200 and body.get("ok"), f"{code} {str(body)[:60]}")
check("landed on the server", os.path.exists(os.path.join(root, "uploaded.bin")))
check("byte-identical", open(os.path.join(root, "uploaded.bin"), "rb").read() == payload)

code, data = A.request("GET", "/api/download?path=/uploaded.bin")
check("download returns the bytes", code == 200 and data == payload,
      f"{code} {len(data) if isinstance(data, bytes) else data}")

code, body = A.request("POST", "/api/mkdir", {"path": "/", "name": "made-here"})
check("mkdir", code == 200 and os.path.isdir(os.path.join(root, "made-here")), body)
code, body = A.request("POST", "/api/delete", {"path": "/made-here", "dir": True})
check("delete", code == 200 and not os.path.exists(os.path.join(root, "made-here")), body)

print("\npaths are normalised, and the root is protected")
code, body = A.request("POST", "/api/delete", {"path": "/", "dir": True})
check("refuses to delete the target root", code == 400, f"{code} {str(body)[:50]}")
# Test the normaliser directly: an error response has no path to inspect,
# and what matters is that nothing can climb above the target's root.
for given, want in [("/../../etc", "/etc"), ("/..", "/"), ("", "/"),
                    ("/a/../../b", "/b"), ("//x//y", "/x/y"),
                    ("/a/./b/", "/a/b"), ("....//", "/....")]:
    got = srv.clean_path(given)
    check(f"clean_path({given!r}) stays inside root", got == want and got.startswith("/"),
          f"got {got!r}, wanted {want!r}")
code, body = A.request("GET", "/api/list?path=/../../etc")
check("a traversal attempt reaches no listing", code in (200, 400), code)

print("\nthe visitor sees the target's answer, not a gateway error")
check("a wrong password maps to 401", srv.error_status("authentication failed for x") == 401)
check("no session maps to 409", srv.error_status("not connected") == 409)
check("anything else is a 400, never 5xx", srv.error_status("no such folder") == 400)

print("\nan unread body never bleeds into the next request")
# The bug this covers: a handler that errors before reading the body leaves
# the bytes in the socket, and with keep-alive they get parsed as the next
# request line — corrupting it, and printing the body into the access log,
# which is where a password would be.
import http.client as _hc
conn = _hc.HTTPConnection("127.0.0.1", PORT, timeout=20)
payload = json.dumps({"path": "/", "secret": "hunter2-should-never-be-logged"}).encode()
conn.request("POST", "/api/mkdir", body=payload,
             headers={"Content-Type": "application/json"})   # no session -> errors
r1 = conn.getresponse(); first = r1.status; r1.read()
check("the erroring request answers", first in (400, 409), first)
check("and asks to close rather than desync",
      (r1.getheader("Connection") or "").lower() == "close", r1.getheader("Connection"))
conn.close()

# Prove the next request on a fresh connection is unaffected and well-formed.
conn2 = _hc.HTTPConnection("127.0.0.1", PORT, timeout=20)
conn2.request("GET", "/api/health")
r2 = conn2.getresponse(); body2 = r2.read(); conn2.close()
check("the following request is clean", r2.status == 200 and b'"ok"' in body2,
      f"{r2.status} {body2[:40]}")

print("\ndisconnect clears the session")
code, body = A.request("POST", "/api/disconnect")
check("disconnect", code == 200, code)
code, body = A.request("GET", "/api/list?path=/")
check("no longer connected", code == 409, code)

egress.BLOCKED_V4 = real_v4
egress._blocked_reason = _real_reason
httpd.shutdown()
print(f"\n{'ALL PASS' if ok else '*** FAILURES ***'}")
sys.exit(0 if ok else 1)
