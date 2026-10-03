#!/usr/bin/env bash
# Start the product container and wait until http://127.0.0.1:13200 answers.
# Env: WORKSPACE_DIR PRODUCT_CONTAINER PRODUCT_IMAGE
# Optional: PRODUCT_PUBLISH_PORT (13200) PRODUCT_CONTAINER_PORT (3000)
#           PRODUCT_WAIT_TIMEOUT (600) PRODUCT_PROBE_TIMEOUT (45)
#           PRED_EGRESS_PROXY_URL
set -euo pipefail

WORKSPACE_DIR="${WORKSPACE_DIR:?WORKSPACE_DIR unset}"
PRODUCT_CONTAINER="${PRODUCT_CONTAINER:?PRODUCT_CONTAINER unset}"
PRODUCT_IMAGE="${PRODUCT_IMAGE:?PRODUCT_IMAGE unset}"
PRODUCT_PUBLISH_PORT="${PRODUCT_PUBLISH_PORT:-13200}"
PRODUCT_CONTAINER_PORT="${PRODUCT_CONTAINER_PORT:-3000}"
PRODUCT_WAIT_TIMEOUT="${PRODUCT_WAIT_TIMEOUT:-600}"
PRODUCT_PROBE_TIMEOUT="${PRODUCT_PROBE_TIMEOUT:-45}"
APP_URL="http://127.0.0.1:${PRODUCT_PUBLISH_PORT}"

if [ ! -d "$WORKSPACE_DIR/.git" ]; then
  echo "workspace is not a git repo: $WORKSPACE_DIR" >&2
  exit 1
fi

proxy_args=()
if [ -n "${PRED_EGRESS_PROXY_URL:-}" ]; then
  proxy_args+=(
    -e "http_proxy=${PRED_EGRESS_PROXY_URL}"
    -e "https_proxy=${PRED_EGRESS_PROXY_URL}"
    -e "HTTP_PROXY=${PRED_EGRESS_PROXY_URL}"
    -e "HTTPS_PROXY=${PRED_EGRESS_PROXY_URL}"
    -e "ALL_PROXY=${PRED_EGRESS_PROXY_URL}"
    -e "all_proxy=${PRED_EGRESS_PROXY_URL}"
    -e "npm_config_proxy=${PRED_EGRESS_PROXY_URL}"
    -e "npm_config_http_proxy=${PRED_EGRESS_PROXY_URL}"
    -e "npm_config_https_proxy=${PRED_EGRESS_PROXY_URL}"
    -e "no_proxy=127.0.0.1,localhost,::1"
    -e "NO_PROXY=127.0.0.1,localhost,::1"
  )
fi

RUNTIME="${RUNTIME:-/runtime}"
bash "${RUNTIME}/release-product-port.sh"
docker rm -f "$PRODUCT_CONTAINER" >/dev/null 2>&1 || true
docker run -d \
  --name "$PRODUCT_CONTAINER" \
  -p "127.0.0.1:${PRODUCT_PUBLISH_PORT}:${PRODUCT_CONTAINER_PORT}" \
  -v "$WORKSPACE_DIR:/workspace/product" \
  "${proxy_args[@]}" \
  "$PRODUCT_IMAGE"

docker exec -d "$PRODUCT_CONTAINER" bash -lc \
  "cd /workspace/product && HOST=0.0.0.0 PORT=${PRODUCT_CONTAINER_PORT} BROWSER=none product-yarn start >/tmp/product-dev-server.log 2>&1"

echo "waiting for ${APP_URL} (max ${PRODUCT_WAIT_TIMEOUT}s)"
deadline=$((SECONDS + PRODUCT_WAIT_TIMEOUT))
while [ "$SECONDS" -lt "$deadline" ]; do
  status="$(curl --noproxy '*' -sS -o /dev/null -w '%{http_code}' --max-time "$PRODUCT_PROBE_TIMEOUT" "$APP_URL" 2>/dev/null || true)"
  if [ -n "$status" ] && [ "$status" != "000" ] && [ "$status" -lt 500 ]; then
    echo "product ready: ${APP_URL}"
    exit 0
  fi
  sleep 5
done

echo "product wait timeout: ${APP_URL}" >&2
docker logs "$PRODUCT_CONTAINER" >&2 || true
docker exec "$PRODUCT_CONTAINER" bash -lc 'tail -n 80 /tmp/product-dev-server.log 2>/dev/null || true' >&2 || true
bash "${RUNTIME:-/runtime}/release-product-port.sh" || true
exit 1
