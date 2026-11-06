#!/usr/bin/env bash
# Run the client tests. Kept for muscle memory; the real runner is
# tests/run_all.py (per-file timeout, no `timeout` binary needed, non-zero
# exit if anything fails). test_sftp/test_queue/test_gui skip unless
# FOXZILLA_TEST_SSH_HOST is set.
exec python3 "$(dirname "$0")/../../tests/run_all.py" client "$@"
