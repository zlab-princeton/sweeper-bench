#!/usr/bin/env bash
# Gemini CLI adapter. Official Gemini API. The outer script writes the patch.
set -euo pipefail

PRODUCT_DIR="${PRODUCT_DIR:-/workspace/product}"
TASK_FILE="${TASK_FILE:-/workspace/task.md}"
LOG_DIR="${PREDICTION_LOG_DIR:-/workspace/output}"
PREDICTION_MODEL="${PREDICTION_MODEL:-gemini-2.5-pro}"
PREDICTION_PROVIDER="${PREDICTION_PROVIDER:-gemini}"
PREDICTION_EFFORT="${PREDICTION_EFFORT:-xhigh}"
GEMINI_HOME="${GEMINI_HOME:-${HOME:-/home/agent}/.gemini}"
mkdir -p "$LOG_DIR" "$GEMINI_HOME"

case "$PREDICTION_PROVIDER" in
  google|gemini)
    : "${GEMINI_API_KEY:?PREDICTION_PROVIDER=${PREDICTION_PROVIDER} requires GEMINI_API_KEY}"
    unset GOOGLE_GEMINI_BASE_URL || true
    ;;
  *)
    echo "gemini-cli accepts only provider gemini or google, got: $PREDICTION_PROVIDER" >&2
    exit 1
    ;;
esac

if ! command -v gemini >/dev/null 2>&1; then
  echo "gemini CLI not found in this image" >&2
  exit 1
fi

python3 - "$GEMINI_HOME/settings.json" "$PREDICTION_MODEL" "$PREDICTION_EFFORT" <<'PY'
import json
import os
import sys

dest, model, effort = sys.argv[1], sys.argv[2], (sys.argv[3] or "").strip().lower()
# thinkingLevel is LOW, MEDIUM, or HIGH. xhigh and max map to HIGH.
level = {"low": "LOW", "medium": "MEDIUM", "high": "HIGH", "xhigh": "HIGH", "max": "HIGH"}.get(effort, "HIGH")
settings = {
    "selectedAuthType": "gemini-api-key",
    "security": {"auth": {"selectedType": "gemini-api-key"}},
    "general": {"disableAutoUpdate": True},
    "modelConfigs": {
        "customAliases": {
            model: {
                "extends": "chat-base-3",
                "modelConfig": {
                    "model": model,
                    "generateContentConfig": {
                        "thinkingConfig": {"thinkingLevel": level}
                    },
                },
            }
        }
    },
}
with open(dest, "w", encoding="utf-8") as handle:
    json.dump(settings, handle, indent=2)
    handle.write("\n")
os.environ["GEMINI_THINKING_LEVEL"] = level
print(f"prediction thinking_level: {level}")
PY

export GEMINI_API_KEY
unset GOOGLE_GEMINI_BASE_URL || true

cd "$PRODUCT_DIR"

echo "Gemini CLI version:"
gemini --version || true
echo "prediction harness: gemini-cli"
echo "prediction provider: ${PREDICTION_PROVIDER}"
echo "prediction model: ${PREDICTION_MODEL}"
echo "prediction effort: ${PREDICTION_EFFORT}"
echo "prediction endpoint: generativelanguage.googleapis.com"

PROMPT="$(<"$TASK_FILE")"
# --yolo auto-approves tools. -p is non-interactive.
gemini --yolo -p "$PROMPT" --model "$PREDICTION_MODEL"
