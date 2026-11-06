"""
Foxzilla - a small, dependency-free file transfer client.

A trimmed-down FileZilla: two panes, a queue, and nothing else.
Speaks SFTP, FTP/FTPS, S3 and WebDAV, plus the local disk.

Runs on any machine with Python 3.9+ and Tk. No pip install, ever:
  - SFTP  drives the OpenSSH `sftp` binary, so it inherits your keys,
          your ssh-agent and your ~/.ssh/config host aliases for free.
  - FTP   stdlib ftplib.
  - S3    hand-rolled SigV4 over urllib.
  - DAV   stdlib http.client + ElementTree.

    python3 foxzilla.py          (or: python3 -m foxzilla)
"""
from __future__ import annotations

import sys as _sys
import types as _types

from . import backends, config, queue, scan, util
from .backends import (CHUNK, Backend, Entry, FTPBackend, LocalBackend,
                       S3Backend, SFTPBackend, TransferError, WebDAVBackend,
                       _http_time, _int, _iso_time, _ls_time, _mlsd_time,
                       _pump, _s3_quote, clean_typed_path, make_backend,
                       safe_name, under_root)
from .config import (CONFIG_DIR, DEFAULT_SITES, SECRETS_FILE, SITES_FILE,
                     load_secrets, load_sites, needs_password, save_secret,
                     save_sites)
from .queue import COMPLETE_SUFFIX, Job, Queue, marker_path
from .scan import (CLEAR, CONFLICT, IDENTICAL, SAME_SIZE, UNSAFE, WANTED, PlanItem,
                   compare_paths, plan_transfer, summarise)
from .util import APP, VERSION, bar, human, when

# The window, imported only when asked for, so the backends, queue and scan
# work on a machine with no Tk (the hosted variant, --check over ssh).
_GUI = {"App", "Pane", "ScanDialog", "SiteDialog", "THEME", "apply_theme",
        "HAVE_TK", "tk", "ttk", "messagebox", "simpledialog"}


def __getattr__(name):
    if name in _GUI:
        from . import gui
        return getattr(gui, name)
    if name in ("main", "check_site", "find_sites"):
        from . import cli
        return getattr(cli, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class _Package(_types.ModuleType):
    """
    Lets `foxzilla.SITES_FILE = ...` keep working after the split.

    Tests (and anything else embedding the client) point the config at a
    scratch directory by assigning these names on the module. They now live
    in config.py, so pass the assignment through to where they are read.
    """
    _FORWARD = ("CONFIG_DIR", "SITES_FILE", "SECRETS_FILE")

    def __setattr__(self, name, value):
        if name in self._FORWARD:
            setattr(config, name, value)
        super().__setattr__(name, value)


_sys.modules[__name__].__class__ = _Package
