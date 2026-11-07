#!/bin/sh
# Recreate the foxzilla-pickup container from the current foxzilla-pickup:latest
# image. Stopping it kills any in-flight handoff: the source is left intact and
# staging is left under <dest>/.staging for inspection, so this is safe but not free.
# /mnt/incoming (not just upload/) is mounted so converted DVD ISOs can be parked
# beside the inbox in /mnt/incoming/.removed-YYYYMMDD on the same filesystem.
# /opt/revproxy/html/pickup is mounted at /status: the pickup writes status.json
# there and the dashboard (served from /opt/revproxy/html) polls it.
# /mnt/nas-media/videos is the Videos library: courses and anything put in the
# inbox's Videos/ or BJJ/ sorting folders.
set -eu
mkdir -p /opt/revproxy/html/pickup
docker rm -f foxzilla-pickup >/dev/null 2>&1 || true
docker run -d --name foxzilla-pickup --restart unless-stopped \
  -e INTERVAL=60 -e TZ=Europe/Dublin -e JELLYFIN_HOST=172.17.0.1:8096 \
  -e UPLOAD_DIR=/incoming/upload -e PARK_DIR=/incoming -e MOVIES_DEST=/movies -e SHOWS_DEST=/tv -e VIDEOS_DEST=/videos \
  -e STATE_FILE=/config/state.json -e LOG_FILE=/config/pickup.log \
  -e RECEIPTS_DIR=/config/receipts -e LOCK_FILE=/tmp/media_pickup.lock \
  -e MEDIA_PICKUP_CREDENTIALS=/config/credentials.json \
  -e STATUS_FILE=/status/status.json \
  -v /srv/foxzilla-config:/config -v /mnt/incoming:/incoming \
  -v /mnt/nas-media/movies:/movies -v /mnt/nas-media/tv:/tv -v /mnt/nas-media/videos:/videos \
  -v /opt/revproxy/html/pickup:/status \
  foxzilla-pickup:latest
