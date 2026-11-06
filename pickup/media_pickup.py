#!/usr/bin/env python3
"""
Media pickup watcher.  v2 - entry point.

The code lives in the foxzilla_pickup package beside this file (see its
__init__ for the three-phase design). This file stays as the entry point the
container, its HEALTHCHECK and the tests call: /app/media_pickup.py.

Every public name is re-exported here for reading (MP.classify, MP.CHUNK...).
Assigning to one here changes nothing the package calls: patch the attribute
on the submodule that looks it up (see tests/test_pickup.py).
"""
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)

from foxzilla_pickup.common import *      # noqa: E402,F401,F403
from foxzilla_pickup.progress import *    # noqa: E402,F401,F403
from foxzilla_pickup.classify import *    # noqa: E402,F401,F403
from foxzilla_pickup.jellyfin import *    # noqa: E402,F401,F403
from foxzilla_pickup.disc import *        # noqa: E402,F401,F403
from foxzilla_pickup.handoff import *     # noqa: E402,F401,F403
from foxzilla_pickup.cli import *         # noqa: E402,F401,F403
from foxzilla_pickup.cli import main      # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
