#!/usr/bin/env bash
# Run the agent-runner prediction container once.
set -euo pipefail

WORKSPACE_DIR="${WORKSPACE_DIR:?WORKSPACE_DIR unset}"
TASK_PROMPT_FILE="${TASK_PROMPT_FILE:?TASK_PROMPT_FILE unset}"
OUTPUT_DIR="${OUTPUT_DIR:?OUTPUT_DIR unset}"
AGENT_IMAGE="${AGENT_IMAGE:?AGENT_IMAGE unset}"
PRODUCT_CONTAINER="${PRODUCT_CONTAINER:?PRODUCT_CONTAINER unset}"
PREDICTION_HARNESS="${PREDICTION_HARNESS:-codex}"
PREDICTION_PROVIDER="${PREDICTION_PROVIDER:-openai}"
PREDICTION_MODEL="${PREDICTION_MODEL:?PREDICTION_MODEL unset}"
RUNTIME="${RUNTIME:-/runtime}"
TASK_SHELL_FILE="${TASK_SHELL_FILE:-$RUNTIME/harnesses/prediction/task_shell.sh}"

mkdir -p "$OUTPUT_DIR"

export PRODUCT_PUBLISH_PORT="${PRODUCT_PUBLISH_PORT:-13200}"
FORWARD_UI="${RUNTIME}/forward-product-ui.py"
FORWARD_UI_HOST="${RUNTIME}/forward-product-ui-host.py"

proxy_args=()
if [ -n "${PRED_EGRESS_PROXY_URL:-}" ]; then
  # No ALL_PROXY: Chromium would send 127.0.0.1:13200 through the allowlist.
  # Do not put host.docker.internal / 172.17.0.1 on NO_PROXY.
  proxy_args+=(
    -e "http_proxy=${PRED_EGRESS_PROXY_URL}"
    -e "https_proxy=${PRED_EGRESS_PROXY_URL}"
    -e "HTTP_PROXY=${PRED_EGRESS_PROXY_URL}"
    -e "HTTPS_PROXY=${PRED_EGRESS_PROXY_URL}"
    -e "no_proxy=127.0.0.1,localhost,::1"
    -e "NO_PROXY=127.0.0.1,localhost,::1"
    -e "NODE_USE_ENV_PROXY=1"
  )
fi

# Product is published on 127.0.0.1:13200. Expose the same port on the docker
# gateway so the in-agent hop to host.docker.internal:13200 can connect.
python3 "$FORWARD_UI_HOST" >/tmp/forward-product-ui-host.log 2>&1 &
for i in $(seq 1 50); do
  python3 -c "
import os, socket, subprocess
p = subprocess.run(['docker','network','inspect','bridge','--format','{{(index .IPAM.Config 0).Gateway}}'], capture_output=True, text=True)
gw = (p.stdout or '').strip() or '172.17.0.1'
s = socket.create_connection((gw, int(os.environ.get('PRODUCT_PUBLISH_PORT','13200'))), 1)
s.close()
" && break
  sleep 0.1
done

docker run --rm \
  --add-host=host.docker.internal:host-gateway \
  "${proxy_args[@]}" \
  -e "PRODUCT_PUBLISH_PORT=${PRODUCT_PUBLISH_PORT}" \
  -e CODEX_AUTH_JSON \
  -e CLAUDE_CODE_OAUTH_TOKEN \
  -e PREDICTION_AUTH="${PREDICTION_AUTH:-api}" \
  -e PREDICTION_HARNESS="$PREDICTION_HARNESS" \
  -e PREDICTION_PROVIDER="$PREDICTION_PROVIDER" \
  -e PREDICTION_MODEL="$PREDICTION_MODEL" \
  -e PREDICTION_EFFORT="${PREDICTION_EFFORT:-xhigh}" \
  -e PREDICTION_WORK_MINUTES="${PREDICTION_WORK_MINUTES:-unlimited}" \
  -e PREDICTION_LEVEL="${PREDICTION_LEVEL:-}" \
  -e PREDICTION_LOG_DIR="${PREDICTION_LOG_DIR:-}" \
  -e PREDICTION_PATCH_FILE="${PREDICTION_PATCH_FILE:-}" \
  -e ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}" \
  -e OPENAI_API_KEY="${OPENAI_API_KEY:-}" \
  -e CODEX_API_KEY="${CODEX_API_KEY:-}" \
  -e CURSOR_API_KEY="${CURSOR_API_KEY:-}" \
  -e KIMI_API_KEY="${KIMI_API_KEY:-}" \
  -e MOONSHOT_API_KEY="${MOONSHOT_API_KEY:-}" \
  -e GEMINI_API_KEY="${GEMINI_API_KEY:-}" \
  -e DEEPSEEK_API_KEY="${DEEPSEEK_API_KEY:-}" \
  -e META_API_KEY="${META_API_KEY:-}" \
  -e MODEL_API_KEY="${MODEL_API_KEY:-}" \
  -e PRODUCT_CONTAINER="$PRODUCT_CONTAINER" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$WORKSPACE_DIR:/workspace/product" \
  -v "$TASK_PROMPT_FILE:/workspace/task.md:ro" \
  -v "$OUTPUT_DIR:/workspace/output" \
  -v "$TASK_SHELL_FILE:/workspace/task-shell:ro" \
  -v "${FORWARD_UI}:/usr/local/bin/forward-product-ui.py:ro" \
  -v "${RUNTIME}/harnesses/prediction/timed.py:/usr/local/bin/prediction-timed.py:ro" \
  -v "${RUNTIME}/prompts/time-budget.md:/usr/local/share/time-budget.md:ro" \
  -v "${RUNTIME}/harnesses/prediction/run.sh:/usr/local/bin/run-agent-prediction.sh:ro" \
  -v "${RUNTIME}/harnesses/prediction/codex.sh:/usr/local/bin/prediction-harness-codex:ro" \
  -v "${RUNTIME}/harnesses/prediction/claude-code.sh:/usr/local/bin/prediction-harness-claude-code:ro" \
  -v "${RUNTIME}/harnesses/prediction/cursor.sh:/usr/local/bin/prediction-harness-cursor:ro" \
  -v "${RUNTIME}/harnesses/prediction/kimi-code.sh:/usr/local/bin/prediction-harness-kimi-code:ro" \
  -v "${RUNTIME}/harnesses/prediction/gemini-cli.sh:/usr/local/bin/prediction-harness-gemini-cli:ro" \
  -v "${RUNTIME}/harnesses/prediction/deepseek-harness.sh:/usr/local/bin/prediction-harness-deepseek-harness:ro" \
  -v "${RUNTIME}/harnesses/prediction/muse-code.sh:/usr/local/bin/prediction-harness-muse-code:ro" \
  "$AGENT_IMAGE" \
  bash -lc 'python3 /usr/local/bin/forward-product-ui.py &
for i in $(seq 1 50); do
  python3 -c "import socket; s=socket.create_connection((\"127.0.0.1\", int(\"${PRODUCT_PUBLISH_PORT:-13200}\")), 1); s.close()" && break
  sleep 0.1
done
exec run-agent-prediction.sh'

echo "agent-runner finished, output: $OUTPUT_DIR"
