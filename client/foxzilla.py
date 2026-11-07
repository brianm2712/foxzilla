#!/usr/bin/env python3
"""
Foxzilla launcher.

The code lives in the `foxzilla/` package beside this file; this shim keeps
`python3 client/foxzilla.py`, the Linux .desktop entry and the Mac bundle's
launcher working unchanged. `python3 -m foxzilla` (from client/) is the same.
"""
import os
import sys

if __name__ == "__main__":
    # Make the package beside this file importable however we were started.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from foxzilla.cli import main
    sys.exit(main())
