#!/bin/sh
# Loop the watcher. The script takes its own flock, so overlapping ticks are
# harmless and a long box-set handoff simply makes the next tick a no-op.
set -eu

args="--upload-dir $UPLOAD_DIR --movies-dest $MOVIES_DEST --shows-dest $SHOWS_DEST \
--state-file $STATE_FILE --log-file $LOG_FILE --lock-file $LOCK_FILE \
--receipts-dir $RECEIPTS_DIR --jellyfin-host $JELLYFIN_HOST"

# Pass through management subcommands: docker run ... --login
if [ "$#" -gt 0 ]; then
    exec python3 /app/media_pickup.py $args "$@"
fi

mkdir -p "$(dirname "$STATE_FILE")" "$RECEIPTS_DIR"

if [ -n "${RUN_ONCE:-}" ]; then
    exec python3 /app/media_pickup.py $args
fi

echo "foxzilla-pickup: watching $UPLOAD_DIR every ${INTERVAL}s"
while true; do
    python3 /app/media_pickup.py $args || echo "foxzilla-pickup: tick failed, retrying next interval"
    sleep "$INTERVAL"
done
