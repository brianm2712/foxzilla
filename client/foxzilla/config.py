"""
Saved sites (~/.config/foxzilla/sites.json) and their secrets.

Secrets - passwords, S3 keys - live in the macOS Keychain when there is one,
under the service name 'foxzilla' with the site name as the account. Where
there is no keychain (Linux), or it refuses, they go to secrets.json at mode
0600, which is plain JSON: obfuscation, not encryption. Either way they are
kept out of sites.json, which is meant to be shareable.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

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
    """The secrets.json fallback store (not the keychain)."""
    try:
        with open(SECRETS_FILE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _write_secrets_file(secrets):
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    tmp = SECRETS_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(secrets, fh, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, SECRETS_FILE)


# -- the macOS Keychain, through /usr/bin/security ---------------------------

SECURITY_BIN = "/usr/bin/security"
KEYCHAIN_SERVICE = APP
USE_KEYCHAIN = None          # None: use it on macOS when the tool is there


def keychain_available():
    if USE_KEYCHAIN is not None:
        return bool(USE_KEYCHAIN) and os.access(SECURITY_BIN, os.X_OK)
    return sys.platform == "darwin" and os.access(SECURITY_BIN, os.X_OK)


def _kc_quote(text):
    """Quote for `security -i`, which splits its input lines like a shell."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _kc_get(account):
    try:
        r = subprocess.run([SECURITY_BIN, "find-generic-password",
                            "-s", KEYCHAIN_SERVICE, "-a", account, "-w"],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return r.stdout[:-1] if r.stdout.endswith("\n") else r.stdout


def _kc_set(account, value):
    """
    Add or update an item. The value goes in on stdin (`security -i`), never
    as an argument, so it does not show up in the process list.
    """
    if "\n" in value or "\r" in value:
        return False
    line = " ".join(["add-generic-password", "-U",
                     "-s", _kc_quote(KEYCHAIN_SERVICE), "-a", _kc_quote(account),
                     "-l", _kc_quote(f"{APP}: {account}"), "-w", _kc_quote(value)])
    try:
        r = subprocess.run([SECURITY_BIN, "-i"], input=line + "\n",
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and _kc_get(account) == value


def _kc_delete(account):
    try:
        subprocess.run([SECURITY_BIN, "delete-generic-password",
                        "-s", KEYCHAIN_SERVICE, "-a", account],
                       capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        pass


# -- the secret store ---------------------------------------------------------

def get_secret(name) -> str:
    """A stored secret, or '' if there is none. Keychain first, then the file."""
    if keychain_available():
        value = _kc_get(name)
        if value is not None:
            return value
    return load_secrets().get(name, "") or ""


def set_secret(name, value) -> str:
    """
    Store a secret. Returns where it went: 'keychain' or 'file'.

    A copy already in secrets.json is removed once the keychain has it, so
    saving again migrates an old plaintext entry.
    """
    if keychain_available() and _kc_set(name, value):
        secrets = load_secrets()
        if name in secrets:
            del secrets[name]
            _write_secrets_file(secrets)
        return "keychain"
    secrets = load_secrets()
    secrets[name] = value
    _write_secrets_file(secrets)
    return "file"


# The old name, from when it was never called.
save_secret = set_secret


def delete_secret(name):
    if keychain_available():
        _kc_delete(name)
    secrets = load_secrets()
    if name in secrets:
        del secrets[name]
        _write_secrets_file(secrets)


def secret_store_name():
    return "the macOS Keychain" if keychain_available() else f"{SECRETS_FILE} (0600)"


# -- site secrets ---------------------------------------------------------------

# Fields of a site that are secrets and are kept out of sites.json.
SITE_SECRET_FIELDS = {"s3": ("access_key", "secret_key", "session_token")}


def secret_key_for(site_name, field):
    """The store's key for a site's secret field (its password is just its name)."""
    return f"{site_name}#{field}"


def site_secret(site, field) -> str:
    """A secret field of a site: from the site itself (old files), else the store."""
    return site.get(field) or get_secret(secret_key_for(site.get("name", ""), field))


def move_site_secrets(sites) -> bool:
    """
    Move secret fields out of the site dicts and into the store, in place.

    Returns True when anything moved, so the caller knows to save sites.json.
    A field whose value cannot be stored is left where it is.
    """
    changed = False
    for site in sites:
        for field in SITE_SECRET_FIELDS.get(site.get("type"), ()):
            value = site.get(field)
            if not value:
                continue
            set_secret(secret_key_for(site["name"], field), value)
            if get_secret(secret_key_for(site["name"], field)) == value:
                del site[field]
                changed = True
    return changed


def rename_site_secrets(old, new):
    """A site was renamed: take its password and secret fields with it."""
    if not old or old == new:
        return
    keys = [(old, new)] + [(secret_key_for(old, f), secret_key_for(new, f))
                           for fields in SITE_SECRET_FIELDS.values() for f in fields]
    for a, b in keys:
        value = get_secret(a)
        if value:
            set_secret(b, value)
            delete_secret(a)


def needs_password(site):
    """Sites we must ask for a password before connecting."""
    kind = site.get("type")
    if kind == "sftp":
        # SFTP defaults to key auth; only ask when the site says otherwise.
        return site.get("auth") == "password"
    return kind in ("ftp", "ftps", "dav", "webdav") and not site.get("anonymous")
