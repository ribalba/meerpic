# meerpic-agent: the half that reads, converts and indexes your photos, and
# runs rclone when you press Sync.
#
# Most of this image is tools, and the reason it is an image at all is one of
# them: ffmpeg. An iPhone records video as HEVC, which no browser on Linux
# plays, so every video gets an H.264 copy (see agent/video.py). Fedora's
# ffmpeg-free can neither decode HEVC nor encode H.264 with libx264; Debian's
# ffmpeg can do both, and has zscale for turning the iPhone's HDR into colours
# that look right on an ordinary screen.
FROM python:3.14-slim

ARG MEERPIC_VERSION=0.0.0
# iCloud Photos arrived in rclone's iclouddrive backend in 1.74; Debian's
# packaged rclone is older than that, so the release binary is fetched and
# checked against its published SHA-256.
ARG RCLONE_VERSION=1.74.3
ARG RCLONE_SHA256_AMD64=dbee7ccd7a5d617e4ed4cd4555c16669b511abfe8d31164f61be35ac9e999bd2
ARG RCLONE_SHA256_ARM64=8f8d47446e061f80c3256659fe8e21f56d72d96aaefe1275d088ea5eb6b42aa7
ARG TARGETARCH

LABEL org.opencontainers.image.title="meerpic-agent" \
      org.opencontainers.image.description="Indexer and iCloud sync for meerpic: exiftool, ffmpeg, rclone, SigLIP" \
      org.opencontainers.image.source="https://github.com/ribalba/meerpic" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.version="${MEERPIC_VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg libimage-exiftool-perl ca-certificates curl unzip \
 && rm -rf /var/lib/apt/lists/*

RUN set -eu; \
    arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "$arch" in \
      amd64) sum="$RCLONE_SHA256_AMD64" ;; \
      arm64) sum="$RCLONE_SHA256_ARM64" ;; \
      *) echo "no rclone checksum pinned for $arch" >&2; exit 1 ;; \
    esac; \
    zip="rclone-v${RCLONE_VERSION}-linux-${arch}.zip"; \
    curl -fsSL -o "/tmp/$zip" "https://downloads.rclone.org/v${RCLONE_VERSION}/$zip"; \
    echo "$sum  /tmp/$zip" | sha256sum -c -; \
    unzip -q "/tmp/$zip" -d /tmp/rclone; \
    install -m 0755 "/tmp/rclone/rclone-v${RCLONE_VERSION}-linux-${arch}/rclone" /usr/local/bin/rclone; \
    rm -rf /tmp/rclone "/tmp/$zip"; \
    rclone version | head -1

WORKDIR /app

COPY agent/requirements.txt /app/agent/requirements.txt
RUN pip install --no-cache-dir -r /app/agent/requirements.txt

COPY core /app/core
COPY agent /app/agent
COPY VERSION /app/VERSION
RUN [ "$MEERPIC_VERSION" = "0.0.0" ] || printf '%s\n' "$MEERPIC_VERSION" > /app/VERSION

RUN useradd --create-home --uid 10001 meerpic && chown -R meerpic /app
USER meerpic

# No healthcheck: the first index of a large library is an hour of honest work,
# and an agent between passes is idle by design. Whether it is alive is a
# question about the heartbeat in `settings`, which the UI already answers.
CMD ["python", "-m", "agent.main"]
