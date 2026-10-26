# Foxzilla pickup — the verifying half of the pipeline.
#
# Pure stdlib plus Debian's ffmpeg, whose dvdvideo demuxer turns a held DVD
# ISO into an MKV. No wheels to trust.
FROM python:3.12-slim

LABEL org.opencontainers.image.title="foxzilla-pickup" \
      org.opencontainers.image.description="Three-phase verified media handoff with a Jellyfin rescan" \
      org.opencontainers.image.source="https://github.com/brianm2712/foxzilla"

# tzdata so log timestamps match the host rather than drifting to UTC;
# ffmpeg (trixie's build has libdvdread/libdvdnav) for DVD images.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY media_pickup.py /app/media_pickup.py
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/media_pickup.py /app/docker-entrypoint.sh

# Mount points. Staging is created inside the destination volumes, so the
# atomic-rename guarantee holds only if each destination is a single volume.
VOLUME ["/incoming", "/movies", "/tv", "/config"]

# The credential lives on the /config volume, not in the container's HOME,
# so a rebuilt or replaced container keeps the operator's login.
ENV MEDIA_PICKUP_CREDENTIALS=/config/credentials.json \
    UPLOAD_DIR=/incoming \
    MOVIES_DEST=/movies \
    SHOWS_DEST=/tv \
    STATE_FILE=/config/state.json \
    LOG_FILE=/config/pickup.log \
    RECEIPTS_DIR=/config/receipts \
    LOCK_FILE=/tmp/media_pickup.lock \
    JELLYFIN_HOST=jellyfin:8096 \
    PARK_DIR= \
    TZ=UTC \
    INTERVAL=60 \
    RUN_ONCE=

HEALTHCHECK --interval=5m --timeout=10s \
    CMD python3 /app/media_pickup.py --whoami || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
