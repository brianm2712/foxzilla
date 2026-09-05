"""A minimal RFC959 server: enough of it to exercise an FTP client."""
import os, shutil, socket, threading, datetime as dt

USER, PASSWORD = "ftpuser", "ftppass"

class Session(threading.Thread):
    def __init__(self, conn, root):
        super().__init__(daemon=True)
        self.conn, self.root, self.cwd = conn, root, "/"
        self.pasv = None; self.rnfr = None; self.authed = False
    def send(self, line):
        self.conn.sendall((line + "\r\n").encode())
    def fs(self, path):
        path = path or "."
        if not path.startswith("/"):
            path = os.path.join(self.cwd, path)
        return os.path.join(self.root, os.path.normpath("/" + path).lstrip("/"))
    def open_data(self):
        sock, _ = self.pasv.accept(); self.pasv.close(); self.pasv = None
        return sock
    def run(self):
        self.send("220 mock ftp ready")
        f = self.conn.makefile("r", encoding="utf-8", newline="\r\n")
        for raw in f:
            cmd, _, arg = raw.strip().partition(" ")
            cmd = cmd.upper()
            try:
                self.handle(cmd, arg)
            except Exception as e:
                self.send(f"550 {e}")
            if cmd == "QUIT":
                break
        try: self.conn.close()
        except OSError: pass
    def handle(self, cmd, arg):
        if cmd == "USER": return self.send("331 need password")
        if cmd == "PASS":
            self.authed = True; return self.send("230 logged in")
        if not self.authed and cmd not in ("QUIT", "FEAT"):
            return self.send("530 not logged in")
        if cmd == "QUIT": return self.send("221 bye")
        if cmd == "FEAT": return self.send("211-Features\r\n MLSD\r\n UTF8\r\n211 end")
        if cmd in ("OPTS", "TYPE", "NOOP"): return self.send("200 ok")
        if cmd == "PWD": return self.send(f'257 "{self.cwd}"')
        if cmd == "CWD":
            target = os.path.normpath("/" + (arg if arg.startswith("/") else self.cwd + "/" + arg))
            if not os.path.isdir(self.fs(target)): return self.send("550 no such dir")
            self.cwd = target; return self.send("250 ok")
        if cmd == "PASV":
            s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(1)
            self.pasv = s
            port = s.getsockname()[1]
            return self.send(f"227 Entering Passive Mode (127,0,0,1,{port>>8},{port&255})")
        if cmd in ("MLSD", "LIST"):
            d = self.fs(arg.strip() or self.cwd)
            if not os.path.isdir(d): 
                self.pasv and self.pasv.close(); self.pasv = None
                return self.send("550 no such dir")
            self.send("150 opening data connection")
            sock = self.open_data()
            lines = []
            for n in sorted(os.listdir(d)):
                p = os.path.join(d, n); st = os.stat(p); isd = os.path.isdir(p)
                if cmd == "MLSD":
                    mt = dt.datetime.fromtimestamp(st.st_mtime, dt.timezone.utc).strftime("%Y%m%d%H%M%S")
                    lines.append(f"type={'dir' if isd else 'file'};size={st.st_size};modify={mt}; {n}")
                else:
                    mt = dt.datetime.fromtimestamp(st.st_mtime).strftime("%b %d %H:%M")
                    perm = "drwxr-xr-x" if isd else "-rw-r--r--"
                    lines.append(f"{perm} 1 own grp {st.st_size:>10} {mt} {n}")
            sock.sendall(("\r\n".join(lines) + "\r\n").encode() if lines else b"")
            sock.close(); return self.send("226 transfer complete")
        if cmd == "SIZE":
            p = self.fs(arg)
            if not os.path.isfile(p): return self.send("550 not a file")
            return self.send(f"213 {os.path.getsize(p)}")
        if cmd == "RETR":
            p = self.fs(arg)
            if not os.path.isfile(p):
                self.pasv and self.pasv.close(); self.pasv = None
                return self.send("550 no such file")
            self.send("150 opening data connection"); sock = self.open_data()
            with open(p, "rb") as fh: shutil.copyfileobj(fh, sock.makefile("wb"))
            sock.close(); return self.send("226 transfer complete")
        if cmd == "STOR":
            p = self.fs(arg)
            if not os.path.isdir(os.path.dirname(p)):
                self.pasv and self.pasv.close(); self.pasv = None
                return self.send("550 no such directory")
            self.send("150 opening data connection"); sock = self.open_data()
            with open(p, "wb") as fh: shutil.copyfileobj(sock.makefile("rb"), fh)
            sock.close(); return self.send("226 transfer complete")
        if cmd == "MKD":
            os.mkdir(self.fs(arg)); return self.send(f'257 "{arg}" created')
        if cmd == "RMD":
            os.rmdir(self.fs(arg)); return self.send("250 ok")
        if cmd == "DELE":
            p = self.fs(arg)
            if not os.path.isfile(p): return self.send("550 no such file")
            os.remove(p); return self.send("250 ok")
        if cmd == "RNFR":
            self.rnfr = self.fs(arg); return self.send("350 ready")
        if cmd == "RNTO":
            os.replace(self.rnfr, self.fs(arg)); return self.send("250 ok")
        self.send("502 not implemented")

def serve(root):
    srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0)); srv.listen(5)
    def loop():
        while True:
            try: conn, _ = srv.accept()
            except OSError: return
            Session(conn, root).start()
    threading.Thread(target=loop, daemon=True).start()
    return srv.getsockname()[1]
