# CodeGuardian Infra Offline Deployment

This document explains how to start the CodeGuardian infrastructure from zero, including the offline deployment flow for servers without external image downloads during startup.

## Requirement

The target server must have a container runtime available:

- Docker Engine
- Docker Compose plugin

Docker Desktop is not required on a Linux server.

## First-Time Startup With Internet

Use this flow when the machine can pull images from external registries.

```bash
cd codeguardian-infra

docker volume create infraestructura_jenkins-data
docker volume create infraestructura_sonarqube_logs
docker volume create infraestructura_sonarqube_data
docker volume create infraestructura_sonarqube_temp
docker volume create infraestructura_sonarqube_extensions

docker compose up -d --build
```

Check the services:

```bash
docker compose ps
```

## Create The Offline Image Bundle

Run this on a machine with access to external registries.

```bash
cd codeguardian-infra
./scripts/export-offline-images.sh
```

On Windows PowerShell:

```powershell
cd codeguardian-infra
.\scripts\export-offline-images.ps1
```

The script creates:

```text
dist/codeguardian-images.tar
```

The bundle contains:

- Jenkins custom image built from this repository
- SonarQube
- Prometheus
- Pushgateway
- Grafana
- `mcp/sonarqube:latest`, used by the CodeGuardian agent during analysis

## Start From Zero On The Offline Server

Copy these items to the target server:

- the `codeguardian-infra` repository
- `dist/codeguardian-images.tar`

Then run:

```bash
cd codeguardian-infra

docker volume create infraestructura_jenkins-data
docker volume create infraestructura_sonarqube_logs
docker volume create infraestructura_sonarqube_data
docker volume create infraestructura_sonarqube_temp
docker volume create infraestructura_sonarqube_extensions

./scripts/import-offline-images.sh
```

Equivalent manual commands:

```bash
docker load -i dist/codeguardian-images.tar
docker compose up -d
```

## Normal Startup After Images Are Loaded

After the images are already loaded on the server:

```bash
cd codeguardian-infra
docker compose up -d
```

This starts the infrastructure without pulling images from external registries.

## Stop And Restart

Stop containers without deleting volumes:

```bash
docker compose down
```

Restart one service:

```bash
docker compose restart grafana
docker compose restart jenkins-blueocean
docker compose restart sonarqube-server
```

## Service URLs

| Service | URL |
| --- | --- |
| Jenkins | <http://localhost:8080> |
| SonarQube | <http://localhost:9000> |
| Prometheus | <http://localhost:9090> |
| Pushgateway | <http://localhost:9091> |
| Grafana | <http://localhost:3000> |

## Notes

- The offline bundle avoids external image downloads during startup.
- The server still needs Docker Engine or a compatible runtime.
- If the Compose image versions change, generate a new `dist/codeguardian-images.tar`.
- Do not delete Docker images on the target server unless you plan to reload the bundle.
