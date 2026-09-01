#!/usr/bin/env sh
set -eu

ARCHIVE="${1:-dist/codeguardian-images.tar}"

docker load -i "$ARCHIVE"
docker compose up -d
