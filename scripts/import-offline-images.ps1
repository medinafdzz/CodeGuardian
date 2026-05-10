param(
    [string]$Archive = "dist/codeguardian-images.tar"
)

$ErrorActionPreference = "Stop"

docker load -i $Archive
docker compose up -d
