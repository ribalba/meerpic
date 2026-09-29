# meerpic-server. The agent is not in here: this image shows your photos and
# never changes one, and it has no ffmpeg, no exiftool and no rclone because
# nothing it does needs them.
FROM python:3.14-slim

ARG MEERPIC_VERSION=0.0.0

LABEL org.opencontainers.image.title="meerpic-server" \
      org.opencontainers.image.description="meerpic: your iCloud photos, searchable, on your own machine" \
      org.opencontainers.image.source="https://github.com/ribalba/meerpic" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.version="${MEERPIC_VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first, so a source edit does not re-resolve the world.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY core /app/core
COPY app /app/app
COPY VERSION /app/VERSION
# The build's own number wins over whatever the tree happened to hold, so an
# image cannot claim a version it was not built from.
RUN [ "$MEERPIC_VERSION" = "0.0.0" ] || printf '%s\n' "$MEERPIC_VERSION" > /app/VERSION

# The image's own unprivileged user. Compose runs the container as you
# instead (see docker-compose.yml), because it reads your pictures and writes
# into your cache; this is what it falls back to when nothing says otherwise.
RUN useradd --create-home --uid 10001 meerpic && chown -R meerpic /app
USER meerpic

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz').status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
