#!/usr/bin/env bash
# Start dockerd in the Modal VM and log in to GHCR. Pull-only; no docker build.
set -eu
mkdir -p /output
ulimit -n 1048576 || true
export DEBIAN_FRONTEND=noninteractive
mkdir -p /etc/docker
if [ ! -f /etc/docker/daemon.json ]; then
  printf '%s\n' '{"default-ulimits":{"nofile":{"Name":"nofile","Hard":1048576,"Soft":1048576}}}' > /etc/docker/daemon.json
fi
if ! docker info >/dev/null 2>&1; then
  rm -f /var/run/docker.pid /var/run/docker.sock
  dockerd >/output/dockerd.log 2>&1 &
  for i in $(seq 1 180); do
    docker info >/dev/null 2>&1 && break
    sleep 1
  done
fi
docker info >/dev/null
if [ -z "${GHCR_TOKEN:-}" ]; then
  echo "GHCR_TOKEN is required" >&2
  exit 1
fi
printf '%s' "$GHCR_TOKEN" | docker login ghcr.io -u "${GHCR_USER:-evigbyen}" --password-stdin
echo "docker/ghcr boot ok"
