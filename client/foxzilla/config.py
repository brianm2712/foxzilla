"""Saved sites and secrets under ~/.config/foxzilla."""
from __future__ import annotations

import json
import os

from .util import APP

CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), APP
)
SITES_FILE = os.path.join(CONFIG_DIR, "sites.json")
SECRETS_FILE = os.path.join(CONFIG_DIR, "secrets.json")


DEFAULT_SITES = [
    {"name": "Local", "type": "local"},
]


def load_sites():
    try:
        with open(SITES_FILE) as fh:
            sites = json.load(fh)
        if isinstance(sites, dict):
            sites = sites.get("sites", [])
        return [s for s in sites if isinstance(s, dict) and s.get("name")]
    except (OSError, ValueError):
        return list(DEFAULT_SITES)


def save_sites(sites):
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    tmp = SITES_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(sites, fh, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, SITES_FILE)


def load_secrets():
    try:
        with open(SECRETS_FILE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_secret(name, value):
    """
    Stash a password so a site connects without prompting.

    This is obfuscation, not encryption - the file is plain JSON at mode 0600.
    Nothing is ever written here unless you explicitly tick 'remember'.
    """
    secrets = load_secrets()
    secrets[name] = value
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    tmp = SECRETS_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(secrets, fh, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, SECRETS_FILE)


def needs_password(site):
    """Sites we must ask for a password before connecting."""
    kind = site.get("type")
    if kind == "sftp":
        # SFTP defaults to key auth; only ask when the site says otherwise.
        return site.get("auth") == "password"
    return kind in ("ftp", "ftps", "dav", "webdav") and not site.get("anonymous")
