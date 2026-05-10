#!/usr/bin/env sh
set -eu

DIST_DIR="${DIST_DIR:-dist}"
ARCHIVE="${ARCHIVE:-$DIST_DIR/codeguardian-images.tar}"
EXTRA_IMAGES="mcp/sonarqube:latest"

mkdir -p "$DIST_DIR"

docker compose pull
docker compose build jenkins-blueocean

for image in $EXTRA_IMAGES; do
  if ! docker image inspect "$image" >/dev/null 2>&1; then
    docker pull "$image"
  fi
done

IMAGES="$(docker compose config --images) $EXTRA_IMAGES"
docker save -o "$ARCHIVE" $IMAGES

echo "Offline image bundle created at $ARCHIVE"
