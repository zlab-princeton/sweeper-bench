#!/usr/bin/env bash
# Make sure the shared product publish port is free before a case starts.
# Previous cases on this VM may still hold 127.0.0.1:13200.
set -u
PORT="${PRODUCT_PUBLISH_PORT:-13200}"

holders() {
  if command -v ss >/dev/null 2>&1; then
    ss -lntp 2>/dev/null | grep -E ":${PORT}[[:space:]]" || true
  elif command -v netstat >/dev/null 2>&1; then
    netstat -lntp 2>/dev/null | grep -E ":${PORT}[[:space:]]" || true
  fi
}

echo "release-product-port: check :${PORT}"
docker ps -aq --filter "name=-product-" | xargs -r docker rm -f >/dev/null 2>&1 || true
docker ps -aq --filter "publish=${PORT}" | xargs -r docker rm -f >/dev/null 2>&1 || true

busy="$(holders)"
if [ -n "$busy" ]; then
  echo "release-product-port: occupied, releasing"
  printf '%s\n' "$busy"
  if command -v fuser >/dev/null 2>&1; then
    fuser -k "${PORT}/tcp" >/dev/null 2>&1 || true
  else
    printf '%s\n' "$busy" | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' | sort -u | xargs -r kill -9 >/dev/null 2>&1 || true
  fi
  sleep 0.4
  docker ps -aq --filter "publish=${PORT}" | xargs -r docker rm -f >/dev/null 2>&1 || true
  docker ps -aq --filter "name=-product-" | xargs -r docker rm -f >/dev/null 2>&1 || true
fi

busy="$(holders)"
if [ -n "$busy" ]; then
  echo "release-product-port: still occupied :${PORT}" >&2
  printf '%s\n' "$busy" >&2
  exit 1
fi
echo "release-product-port: :${PORT} is free"
