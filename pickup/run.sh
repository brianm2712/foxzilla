#!/bin/sh
# Recreate the foxzilla-pickup container from the current foxzilla-pickup:latest
# image. Stopping it kills any in-flight handoff: the source is left intact and
# staging is left under <dest>/.staging for inspection, so this is safe but not free.
set -eu
docker rm -f foxzilla-pickup >/dev/null 2>&1 || true
docker run -d --name foxzilla-pickup --restart unless-stopped \
  -e INTERVAL=60 -e TZ=Europe/Dublin -e JELLYFIN_HOST=172.17.0.1:8096 \
  -e UPLOAD_DIR=/incoming -e MOVIES_DEST=/movies -e SHOWS_DEST=/tv \
  -e STATE_FILE=/config/state.json -e LOG_FILE=/config/pickup.log \
  -e RECEIPTS_DIR=/config/receipts -e LOCK_FILE=/tmp/media_pickup.lock \
  -e MEDIA_PICKUP_CREDENTIALS=/config/credentials.json \
  -v /srv/foxzilla-config:/config -v /mnt/incoming/upload:/incoming \
  -v /mnt/nas-media/movies:/movies -v /mnt/nas-media/tv:/tv \
  foxzilla-pickup:latest
