#!/usr/bin/env python3
"""
Run every test file in the repo and fail if any of them fails.

    python3 tests/run_all.py                 # everything
    python3 tests/run_all.py client web      # only these top-level areas
    python3 tests/run_all.py -k ftp          # only files whose path contains 'ftp'
    python3 tests/run_all.py -v              # show each file's full output
    python3 tests/run_all.py --python /path/to/python3   # e.g. one with Tk

Each file runs in its own interpreter with a timeout, because the test files
are plain scripts (no framework) and one hung socket must not hang the run.
A file passes when it exits 0. A file that prints a line starting with
"skipped:" and exits 0 is reported as SKIP - the live-host tests do that when
FOXZILLA_TEST_SSH_HOST is unset. Stdlib only.

Some client tests bind loopback sockets or open a pty. Under a sandbox that
forbids either they fail rather than skip; run them outside it.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AREAS = ("client", "web", "build", "pickup")


def discover(areas, needle=None):
    found = []
    for area in areas:
        base = os.path.join(ROOT, area)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames
                                 if not d.startswith((".", "__")))
            for f in sorted(filenames):
                if f.startswith("test_") and f.endswith(".py"):
                    path = os.path.join(dirpath, f)
                    if needle and needle not in os.path.relpath(path, ROOT):
                        continue
                    found.append(path)
    return found


def run_one(path, timeout, python=sys.executable):
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [python, path], cwd=os.path.dirname(path),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, timeout=timeout,
            # unbuffered, so a file that times out still shows how far it got
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        out = proc.stdout.decode("utf-8", "replace")
        rc = proc.returncode
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode("utf-8", "replace")
        out += f"\n*** timed out after {timeout}s ***\n"
        rc = None
    took = time.monotonic() - started
    if rc == 0:
        skipped = any(line.startswith("skipped:") for line in out.splitlines())
        status = "SKIP" if skipped else "PASS"
    else:
        status = "TIME" if rc is None else "FAIL"
    return status, rc, out, took


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("areas", nargs="*", default=list(AREAS),
                    help=f"top-level dirs to search (default: {' '.join(AREAS)})")
    ap.add_argument("-k", dest="needle", help="only paths containing this text")
    ap.add_argument("-t", "--timeout", type=int, default=300,
                    help="seconds per file (default 300)")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--python", default=os.environ.get("FOXZILLA_TEST_PYTHON",
                                                       sys.executable),
                    help="interpreter to run the tests with (default: this one, "
                         "or $FOXZILLA_TEST_PYTHON)")
    args = ap.parse_args(argv)

    files = discover(args.areas, args.needle)
    if not files:
        print("no test files found")
        return 2

    results = []
    for path in files:
        rel = os.path.relpath(path, ROOT)
        status, rc, out, took = run_one(path, args.timeout, args.python)
        results.append((rel, status))
        print(f"{status}  {rel}  ({took:.1f}s)", flush=True)
        if args.verbose or status in ("FAIL", "TIME"):
            tail = out.rstrip().splitlines()
            shown = tail if args.verbose else tail[-25:]
            for line in shown:
                print(f"    | {line}")

    bad = [r for r, s in results if s in ("FAIL", "TIME")]
    counts = {s: sum(1 for _, x in results if x == s)
              for s in ("PASS", "SKIP", "FAIL", "TIME")}
    print("\n" + ", ".join(f"{n} {s.lower()}" for s, n in counts.items() if n))
    if bad:
        print("FAILED: " + ", ".join(bad))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
