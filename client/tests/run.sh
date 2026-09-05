#!/usr/bin/env bash
# Run every test. test_sftp/test_queue/test_gui need FOXZILLA_TEST_SSH_HOST set;
# test_ftp and test_dav_s3 are self-contained (they spin up their own servers).
cd "$(dirname "$0")"
for t in test_ftp test_dav_s3 test_sftp test_queue test_gui; do
    echo "===== $t ====="
    timeout 300 python3 "$t.py" 2>&1 | tail -6
done
