#!/usr/bin/env bash
# Muse adapter. Official META_API_KEY at api.meta.ai. No local proxy.
set -euo pipefail

PRODUCT_DIR="${PRODUCT_DIR:-/workspace/product}"
TASK_FILE="${TASK_FILE:-/workspace/task.md}"
PREDICTION_PROVIDER="${PREDICTION_PROVIDER:-meta}"
PREDICTION_EFFORT="${PREDICTION_EFFORT:-xhigh}"

case "$PREDICTION_PROVIDER" in
  meta|muse)
    PREDICTION_MODEL="${PREDICTION_MODEL:-muse-spark-1.3}"
    export META_API_KEY="${META_API_KEY:-${MODEL_API_KEY:-}}"
    if [ -z "$META_API_KEY" ]; then
      echo "muse-code provider=${PREDICTION_PROVIDER} requires META_API_KEY or MODEL_API_KEY" >&2
      exit 1
    fi
    ;;
  *)
    echo "muse-code accepts only provider meta or muse, got: $PREDICTION_PROVIDER" >&2
    exit 1
    ;;
esac

if ! command -v muse >/dev/null 2>&1; then
  echo "muse CLI not found in this image" >&2
  exit 1
fi

cd "$PRODUCT_DIR"

echo "Muse Code version:"
muse --version || true
echo "prediction harness: muse-code"
echo "prediction provider: ${PREDICTION_PROVIDER}"
echo "prediction model: ${PREDICTION_MODEL}"
echo "prediction effort: ${PREDICTION_EFFORT}"

# --yolo skips approval and Muse's sandbox. The container is already isolated.
export META_API_KEY
muse exec \
  --yolo \
  --model "$PREDICTION_MODEL" \
  --reasoning-effort "$PREDICTION_EFFORT" \
  --prompt-file "$TASK_FILE"
