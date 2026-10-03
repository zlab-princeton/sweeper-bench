#!/usr/bin/env bash
# Cursor adapter. Uses CURSOR_API_KEY and the official Cursor backend.
set -euo pipefail

# Paths come from run-agent-prediction.sh.
PRODUCT_DIR="${PRODUCT_DIR:-/workspace/product}"
TASK_FILE="${TASK_FILE:-/workspace/task.md}"
PREDICTION_MODEL="${PREDICTION_MODEL:-grok-4.6}"

find_cursor_agent() {
  # The image may install either name.
  if command -v agent >/dev/null 2>&1; then
    command -v agent
    return 0
  fi
  if command -v cursor-agent >/dev/null 2>&1; then
    command -v cursor-agent
    return 0
  fi
  return 1
}

CURSOR_AGENT="$(find_cursor_agent || true)"
if [ -z "$CURSOR_AGENT" ]; then
  echo "Cursor Agent CLI not found in this image" >&2
  exit 1
fi

if [ -z "${CURSOR_API_KEY:-}" ]; then
  echo "warning: CURSOR_API_KEY is unset; using any login already in the image" >&2
fi

cd "$PRODUCT_DIR"

echo "Cursor agent version:"
"$CURSOR_AGENT" --version || true
echo "prediction harness: cursor"
echo "prediction provider: cursor"
echo "prediction model: ${PREDICTION_MODEL}"

# --force auto-approves commands. --sandbox disabled turns off Cursor's own sandbox.
PROMPT="$(<"$TASK_FILE")"
export CURSOR_API_KEY
"$CURSOR_AGENT" -p --force --sandbox disabled --trust --workspace "$PRODUCT_DIR" --model "$PREDICTION_MODEL" "$PROMPT"
