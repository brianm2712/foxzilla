# Foxzilla, hosted

The desktop client's two panes on the web: a visitor configures their own
remote, browses it beside their own files, and transfers between the two with
the same scan-and-dedup step the desktop version does.

```
python3 web/server.py            # listens on 127.0.0.1:8110
```

Standard library only, and it imports `client/foxzilla.py` directly for the
backends rather than reimplementing them.

## Why hosting this is different

Shipping a binary means the user's machine opens the connection. Hosting it
means **this server** does, on a stranger's instructions — so by default it is
a way to reach anything the server can reach. That is the whole risk, and it
is handled in one place, `egress.py`:

- The **name is not trusted, the answer is.** A host is resolved first, and
  refused if *any* answer lands in a private range — loopback, RFC1918,
  `100.64/10` (Tailscale), link-local and cloud metadata, multicast, and the
  v4-mapped IPv6 forms of all of them.
- The caller is handed **the checked address**, not the name, and connects to
  that. Re-resolving at connect time would reopen the door: a name can answer
  publicly for the check and privately a moment later.
- Only file-transfer ports are permitted. No 3306, no 6379, no 8091.

Two more rules, in `server.py`:

- **Credentials belong to the visitor.** They live in memory for the life of a
  session, and are never written to disk, logged, or returned to the browser.
- **"Local" is the visitor's machine**, which a server cannot browse. Files
  arrive by upload and leave by download; the server holds one only while it
  is in flight.

## The scan

Files are hashed **in the browser**, with SubtleCrypto, before anything moves.
The scan sends only name, size and digest, so nothing is uploaded to discover
it was already there — which is the entire point.

The server compares size first, then asks the far side for a hash, and only
when both sides can produce one does it say *identical*. Where it cannot — an
FTP server, a chrooted SFTP account — the honest verdict is **"same size"**,
and the dialog says so rather than implying certainty.

## Tests

```
python3 web/tests/test_egress.py
python3 web/tests/test_server.py
```

`test_egress` runs the guard at full strength against every range that
matters. `test_server` covers refusals at full strength, then **deliberately
relaxes the guard** so the application behind it can be exercised against a
local mock at all — relaxing it in a test is fine, relaxing it in the service
would defeat the point.
