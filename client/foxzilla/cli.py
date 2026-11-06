"""Command line: --list, --check, and starting the window."""
from __future__ import annotations

import os
import sys

from . import config
from .backends import make_backend
from .config import DEFAULT_SITES, load_secrets, load_sites, needs_password, save_sites
from .util import human

USAGE = __import__("foxzilla").__doc__


def find_sites(sites, name):
    """
    Sites a name from the command line could mean, ignoring case.

    An exact name wins outright; otherwise any site whose name contains it,
    so `--check mediadrop` finds "mediadrop (proxmox)" without the quoting.
    """
    want = name.strip().lower()
    exact = [x for x in sites if x["name"].lower() == want]
    if exact or not want:
        return exact
    return [x for x in sites if want in x["name"].lower()]


def check_site(name):
    """
    Connect to one site from the terminal and report what happened.

        python3 foxzilla.py --check mediadrop

    Exists because a GUI can only say "it didn't connect". This prints the
    actual dialogue with the server - with the password redacted - so a
    failure can be read rather than guessed at.
    """
    import getpass

    sites = load_sites()
    matches = find_sites(sites, name)
    if not matches:
        print(f"No site matches {name!r}. Known: "
              + ", ".join(x["name"] for x in sites))
        return 1
    if len(matches) > 1:
        print(f"{name!r} matches more than one site - be more specific: "
              + ", ".join(x["name"] for x in matches))
        return 1
    site = matches[0]

    print(f"site      : {site['name']}  ({site.get('type')})")
    for k in ("host", "user", "port", "auth", "url", "bucket", "path"):
        if site.get(k):
            print(f"{k:10}: {site[k]}")

    password = ""
    if needs_password(site):
        password = load_secrets().get(site["name"], "")
        if not password:
            password = getpass.getpass("password  : ")

    backend = make_backend(site, password)
    print(f"control   : {getattr(backend, '_ctl', '(n/a)')}")
    try:
        backend.connect()
    except Exception as e:                                   # noqa: BLE001
        print(f"\nFAILED    : {e}")
        for line in getattr(backend, "transcript", []):
            print("  | " + (line.replace(password, "***") if password else line))
        print("\nIf no password prompt appeared, the server may want a key "
              "instead — clear the site's Auth field to use key auth.")
        return 1

    print(f"connected : home is {backend.home()}")
    try:
        entries = backend.listdir(backend.home())
    except Exception as e:                                   # noqa: BLE001
        print(f"listing   : FAILED — {e}")
        return 1
    print(f"listing   : {len(entries)} entries in {backend.home()}")
    for e in sorted(entries, key=lambda e: e.sort_key)[:15]:
        kind = "dir " if e.is_dir else "file"
        print(f"  {kind} {human(e.size):>8}  {e.name}")
    if not entries:
        print("  (empty — if you expected a folder here, check the account's "
              "chroot and that it can read the directory)")
    try:
        backend.close()
    except Exception:                                        # noqa: BLE001
        pass
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("--check", "-c"):
        if len(argv) < 2:
            print("usage: foxzilla.py --check <site name>")
            return 2
        return check_site(argv[1])
    if argv and argv[0] in ("--list", "-l"):
        for x in load_sites():
            print(f"  {x['name']:24} {x.get('type')}  {x.get('host', x.get('path', ''))}")
        return 0
    if argv and argv[0] in ("--help", "-h"):
        print(USAGE)
        print("  --list            show configured sites")
        print("  --check <site>    connect to one site and report what happened")
        return 0

    from .gui import HAVE_TK, App, tk
    if not HAVE_TK:
        print("Foxzilla's window needs Tkinter — install python3-tk, or use "
              "--list / --check, which work without it.", file=sys.stderr)
        return 1

    if not os.path.exists(config.SITES_FILE):
        save_sites(DEFAULT_SITES)
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0
