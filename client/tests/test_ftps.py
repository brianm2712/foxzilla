#!/usr/bin/env python3
"""
FTPS checks the server's certificate, unless a site explicitly opts out.

A tiny loopback server answers AUTH TLS with a throwaway self-signed
certificate, so the real handshake is exercised: refused by default, accepted
with the site's own CA file, accepted with insecure_tls. Needs the `openssl`
command to mint the certificate; skips without it.
"""
import os, shutil, socket, ssl, subprocess, sys, tempfile, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import foxzilla as F
from foxzilla.backends import ftp as ftpmod

if not shutil.which("openssl"):
    print("skipped: no openssl to mint a test certificate")
    raise SystemExit(0)

ok = True
def check(l, c, e=""):
    global ok
    print(("  PASS " if c else "  FAIL ") + l + (f"   {e}" if e else "")); ok = ok and bool(c)

d = tempfile.mkdtemp()
cert, key = os.path.join(d, "c.pem"), os.path.join(d, "k.pem")
subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", key, "-out", cert, "-days", "1", "-subj", "/CN=127.0.0.1",
                "-addext", "subjectAltName=IP:127.0.0.1"],
               check=True, capture_output=True)

server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
server_ctx.load_cert_chain(cert, key)
lsock = socket.socket(); lsock.bind(("127.0.0.1", 0)); lsock.listen(8)
PORT = lsock.getsockname()[1]
logins = []

def session(conn):
    """Greeting, AUTH TLS, then just enough to log in. No data channel."""
    try:
        conn.sendall(b"220 tls mock\r\n")
        f = conn.makefile("rb")
        if not f.readline().upper().startswith(b"AUTH TLS"):
            return
        conn.sendall(b"234 go ahead\r\n")
        tls = server_ctx.wrap_socket(conn, server_side=True)
        tf = tls.makefile("rb")
        while True:
            line = tf.readline().decode().strip()
            if not line:
                return
            cmd = line.split()[0].upper()
            if cmd == "USER":
                tls.sendall(b"331 password\r\n")
            elif cmd == "PASS":
                logins.append(line); tls.sendall(b"230 in\r\n")
            elif cmd in ("PBSZ", "PROT", "OPTS", "TYPE"):
                tls.sendall(b"200 ok\r\n")
            elif cmd == "PWD":
                tls.sendall(b'257 "/"\r\n')
            elif cmd == "QUIT":
                tls.sendall(b"221 bye\r\n"); return
            else:
                tls.sendall(b"502 no\r\n")
    except (OSError, ssl.SSLError):
        pass
    finally:
        try: conn.close()
        except OSError: pass

def accept_loop():
    while True:
        try:
            c, _ = lsock.accept()
        except OSError:
            return
        threading.Thread(target=session, args=(c,), daemon=True).start()
threading.Thread(target=accept_loop, daemon=True).start()

def attempt(**kw):
    b = F.FTPBackend(host="127.0.0.1", port=PORT, user="u", password="secret", tls=True, **kw)
    try:
        b.connect()
        home = b.home()
        b.close()
        return True, home
    except Exception as e:                                   # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"

print("\nthe TLS context")
ctx = F.FTPBackend(host="h", tls=True)._tls_context()
check("verifies certificates by default", ctx.verify_mode == ssl.CERT_REQUIRED)
check("checks the host name by default", ctx.check_hostname)
ctx = F.FTPBackend(host="h", tls=True, insecure_tls=True)._tls_context()
check("insecure_tls turns both off", ctx.verify_mode == ssl.CERT_NONE and not ctx.check_hostname)

print("\nagainst a self-signed server")
before = len(logins)
good, why = attempt()
check("refused by default", not good and "CERTIFICATE_VERIFY_FAILED" in why.upper()
      .replace(" ", "_"), why[:90])
check("and the password was never sent", len(logins) == before, logins)
good, why = attempt(ca_file=cert)
check("accepted when the site names that CA file", good, why)
good, why = attempt(insecure_tls=True)
check("accepted with insecure_tls", good, why)

print("\nsites carry the settings through")
b = F.make_backend({"name": "x", "type": "ftps", "host": "h", "ca_file": cert})
check("ca_file from the site", b.ca_file == cert and not b.insecure_tls)
b = F.make_backend({"name": "x", "type": "ftps", "host": "h", "insecure_tls": "yes"})
check("insecure_tls from the site (as typed in the dialog)", b.insecure_tls is True)
b = F.make_backend({"name": "x", "type": "ftps", "host": "h", "insecure_tls": "no"})
check("'no' means no", b.insecure_tls is False)
b = F.make_backend({"name": "x", "type": "ftp", "host": "h"})
check("plain FTP is untouched", not b.tls)

lsock.close()
print(f"\n{'ALL PASS' if ok else '*** FAILURES ***'}")
sys.exit(0 if ok else 1)
