#!/usr/bin/env bash
# Kimi adapter. Official api.moonshot.cn. The outer script writes the patch.
set -euo pipefail

PRODUCT_DIR="${PRODUCT_DIR:-/workspace/product}"
TASK_FILE="${TASK_FILE:-/workspace/task.md}"
LOG_DIR="${PREDICTION_LOG_DIR:-/workspace/output}"
PREDICTION_MODEL="${PREDICTION_MODEL:-kimi-k3}"
PREDICTION_PROVIDER="${PREDICTION_PROVIDER:-kimi}"
PREDICTION_EFFORT="${PREDICTION_EFFORT:-xhigh}"
if [ -z "${KIMI_MAX_CONTEXT_SIZE:-}" ]; then
  if [ "${PREDICTION_MODEL}" = "kimi-k3" ]; then
    KIMI_MAX_CONTEXT_SIZE=1000000
  else
    KIMI_MAX_CONTEXT_SIZE=200000
  fi
fi
KIMI_CODE_HOME="${KIMI_CODE_HOME:-${HOME:-/home/agent}/.kimi-code}"
mkdir -p "$LOG_DIR" "$KIMI_CODE_HOME"

case "$PREDICTION_PROVIDER" in
  kimi)
    : "${KIMI_API_KEY:=${MOONSHOT_API_KEY:-}}"
    : "${KIMI_API_KEY:?PREDICTION_PROVIDER=kimi requires KIMI_API_KEY or MOONSHOT_API_KEY}"
    PROVIDER_TYPE="kimi"
    PROVIDER_KEY="$KIMI_API_KEY"
    # Official platform.kimi.com is api.moonshot.cn.
    PROVIDER_URL="https://api.moonshot.cn/v1"
    ;;
  *)
    echo "kimi-code accepts only provider kimi, got: $PREDICTION_PROVIDER" >&2
    exit 1
    ;;
esac

if ! command -v kimi >/dev/null 2>&1; then
  echo "kimi CLI not found in this image" >&2
  exit 1
fi

export KIMI_CODE_HOME PREDICTION_EFFORT KIMI_MAX_CONTEXT_SIZE PREDICTION_MODEL
export PROVIDER_TYPE PROVIDER_KEY PROVIDER_URL

python3 - "$KIMI_CODE_HOME/config.toml" <<'PY'
import json
import os
import sys

dest = sys.argv[1]
effort = os.environ["PREDICTION_EFFORT"]
ctx = int(os.environ["KIMI_MAX_CONTEXT_SIZE"])
body = (
    "default_model = \"pred\"\n"
    "default_permission_mode = \"auto\"\n"
    "\n"
    "[thinking]\n"
    f"effort = {json.dumps(effort)}\n"
    "\n"
    "[providers.prediction]\n"
    f"type = {json.dumps(os.environ['PROVIDER_TYPE'])}\n"
    f"api_key = {json.dumps(os.environ['PROVIDER_KEY'])}\n"
    f"base_url = {json.dumps(os.environ['PROVIDER_URL'])}\n"
    "\n"
    "[models.pred]\n"
    "provider = \"prediction\"\n"
    f"model = {json.dumps(os.environ['PREDICTION_MODEL'])}\n"
    f"max_context_size = {ctx}\n"
    "support_efforts = [\"low\", \"medium\", \"high\", \"xhigh\", \"max\"]\n"
    f"default_effort = {json.dumps(effort)}\n"
)
with open(dest, "w", encoding="utf-8") as handle:
    handle.write(body)
PY

cat >"$KIMI_CODE_HOME/tui.toml" <<'EOF'
[upgrade]
auto_install = false
EOF

cd "$PRODUCT_DIR"

echo "Kimi Code version:"
kimi --version || true
echo "prediction harness: kimi-code"
echo "prediction provider: ${PREDICTION_PROVIDER}"
echo "prediction model: ${PREDICTION_MODEL}"
echo "prediction effort: ${PREDICTION_EFFORT}"
echo "prediction endpoint: ${PROVIDER_URL}"

PROMPT="$(<"$TASK_FILE")"
# -p already auto-approves. Do not add --yolo.
kimi -p "$PROMPT" -m pred
