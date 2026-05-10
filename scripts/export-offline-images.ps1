param(
    [string]$Archive = "dist/codeguardian-images.tar"
)

$ErrorActionPreference = "Stop"

$distDir = Split-Path -Parent $Archive
if ($distDir) {
    New-Item -ItemType Directory -Force -Path $distDir | Out-Null
}

docker compose pull
docker compose build jenkins-blueocean

$extraImages = @("mcp/sonarqube:latest")
foreach ($image in $extraImages) {
    docker image inspect $image *> $null
    if ($LASTEXITCODE -ne 0) {
        docker pull $image
    }
}

$composeImages = docker compose config --images
$images = @($composeImages) + $extraImages
docker save -o $Archive @images

Write-Host "Offline image bundle created at $Archive"
