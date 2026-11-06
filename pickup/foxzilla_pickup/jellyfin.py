"""Jellyfin: the credential file, login/logout/whoami, auth headers, rescan, movie lookup."""
from __future__ import annotations

import json
import os
import sys
import time
import unicodedata
import urllib.error
import urllib.request


# Credentials are resolved at runtime (env, then a 0600 config file) so that no
# password lives in this source file.
CRED_FILE = os.environ.get(
    "MEDIA_PICKUP_CREDENTIALS",
    os.path.expanduser("~/.config/media_pickup/credentials.json"))


# --------------------------------------------------------------------------
# Jellyfin
# --------------------------------------------------------------------------

def read_cred_file():
    try:
        with open(CRED_FILE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def write_cred_file(data):
    os.makedirs(os.path.dirname(CRED_FILE), mode=0o700, exist_ok=True)
    tmp = CRED_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CRED_FILE)
    os.chmod(os.path.dirname(CRED_FILE), 0o700)


def load_credentials(host=None, log=None):
    """
    Resolve a Jellyfin credential. No password is ever stored or embedded.

    Order: environment, then the 0600 credential file written by --login.
    What --login persists is the *access token* Jellyfin hands back, so each
    operator authenticates on their own behalf and their password never
    reaches disk. A legacy file containing a password is migrated here: the
    password is exchanged for a token once and then erased.
    """
    for key, field in (("JELLYFIN_API_KEY", "api_key"), ("JELLYFIN_TOKEN", "token")):
        if os.environ.get(key):
            return {field: os.environ[key], "user": os.environ.get("JELLYFIN_USER")}
    env_user, env_pass = os.environ.get("JELLYFIN_USER"), os.environ.get("JELLYFIN_PASSWORD")
    if env_user and env_pass and host:
        # Supplied for this process only; still exchanged rather than kept.
        return {"token": jf_authenticate(host, env_user, env_pass), "user": env_user}

    creds = read_cred_file()
    if creds.get("password") and host:
        try:
            token = jf_authenticate(host, creds["user"], creds["password"])
        except Exception as e:
            log and log(f"could not migrate stored password to a token: {e}", "WARN")
            return creds
        creds = {"user": creds.get("user"), "token": token,
                 "created": time.time(), "migrated_from_password": True}
        write_cred_file(creds)
        log and log("exchanged the stored password for an access token and erased it")
    return creds


def cmd_login(host, user=None, api_key=None):
    """
    Authenticate on your own behalf and store only the resulting token.

    Run it as yourself: `media_pickup.py --login`. Your password is sent to
    Jellyfin once and never written to disk.
    """
    import getpass
    if api_key:
        write_cred_file({"api_key": api_key, "created": time.time()})
        print(f"Stored Jellyfin API key in {CRED_FILE} (0600).")
        return 0
    user = user or input("Jellyfin username: ").strip()
    if not user:
        print("No username given.", file=sys.stderr)
        return 1
    password = getpass.getpass(f"Jellyfin password for {user}: ")
    try:
        token = jf_authenticate(host, user, password)
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError) as e:
        print(f"Login failed: {e}", file=sys.stderr)
        return 1
    finally:
        del password
    write_cred_file({"user": user, "token": token, "created": time.time()})
    print(f"Logged in as {user}. Access token stored in {CRED_FILE} (0600); "
          "your password was not saved.")
    return 0


def cmd_logout():
    try:
        os.remove(CRED_FILE)
        print(f"Removed {CRED_FILE}.")
    except FileNotFoundError:
        print("No stored credential.")
    return 0


def cmd_whoami():
    creds = read_cred_file()
    if not creds:
        print("Not logged in. Run:  media_pickup.py --login")
        return 1
    if creds.get("api_key"):
        print("Authenticated with a Jellyfin API key.")
    else:
        when = time.strftime("%Y-%m-%d %H:%M",
                             time.localtime(creds.get("created", 0)))
        print(f"Logged in as {creds.get('user')} (token stored {when}).")
    if creds.get("password"):
        print("WARNING: this file still contains a password; it will be "
              "exchanged for a token on the next run.")
    return 0


def jf_headers(token=None):
    auth = ('MediaBrowser Client="MediaPickup", Device="MediaPickup", '
            'DeviceId="media-pickup-1", Version="2.0.0"')
    if token:
        auth += f', Token="{token}"'
    return {"X-Emby-Authorization": auth}


def jf_authenticate(host, user, password):
    req = urllib.request.Request(
        f"http://{host}/Users/AuthenticateByName",
        data=json.dumps({"Username": user, "Pw": password}).encode(),
        headers={**jf_headers(), "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)["AccessToken"]


def jf_rescan(host, creds, log):
    """Trigger a library refresh with whatever credential is configured."""
    try:
        if creds.get("api_key"):
            headers = {"X-Emby-Token": creds["api_key"]}
        else:
            token = creds.get("token")
            if not token:
                log("no Jellyfin credential - run 'media_pickup.py --login'; "
                    "rescan skipped", "WARN")
                return
            headers = jf_headers(token)
        req = urllib.request.Request(f"http://{host}/Library/Refresh",
                                     headers=headers, method="POST", data=b"")
        with urllib.request.urlopen(req, timeout=15) as r:
            log(f"triggered Jellyfin rescan (HTTP {r.status})")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            log("Jellyfin rejected the stored token - run "
                "'media_pickup.py --login' to re-authenticate", "WARN")
        else:
            log(f"could not trigger Jellyfin rescan: {e}", "WARN")
    except (urllib.error.URLError, KeyError) as e:
        log(f"could not trigger Jellyfin rescan: {e}", "WARN")


def fold(s):
    """Accent- and case-insensitive form for comparing titles."""
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c)).casefold().strip()


def pick_movie_match(results, title):
    """
    First TheMovieDb result whose name starts with ours -> (name, year).

    An exact match keeps the provider's spelling (accents); a longer provider
    title ("My Left Foot: The Story of ...") keeps our title and takes the year.
    """
    for r in results or []:
        if r.get("SearchProviderName") != "TheMovieDb" or not r.get("ProductionYear"):
            continue
        name = r.get("Name") or ""
        if fold(name) == fold(title):
            return name, int(r["ProductionYear"])
        if fold(name).startswith(fold(title)):
            return title, int(r["ProductionYear"])
    return None


def jf_auth_headers(creds):
    if creds.get("api_key"):
        return {"X-Emby-Token": creds["api_key"]}
    if creds.get("token"):
        return jf_headers(creds["token"])
    return None


def jf_movie_lookup(host, creds, title, log):
    """Ask Jellyfin's metadata providers for the film's year. None when unsure."""
    headers = jf_auth_headers(creds)
    if not headers:
        log("  no Jellyfin credential - run 'media_pickup.py --login' to get years for discs", "WARN")
        return None
    req = urllib.request.Request(
        f"http://{host}/Items/RemoteSearch/Movie",
        data=json.dumps({"SearchInfo": {"Name": title}}).encode(),
        headers={**headers, "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return pick_movie_match(json.load(r), title)
