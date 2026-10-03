#!/usr/bin/env bash
# DeepSeek adapter. Official api.deepseek.com. The outer script writes the patch.
set -euo pipefail

PRODUCT_DIR="${PRODUCT_DIR:-/workspace/product}"
TASK_FILE="${TASK_FILE:-/workspace/task.md}"
LOG_DIR="${PREDICTION_LOG_DIR:-/workspace/output}"
PREDICTION_MODEL="${PREDICTION_MODEL:-deepseek-v4.1-flash}"
PREDICTION_PROVIDER="${PREDICTION_PROVIDER:-deepseek}"
PREDICTION_EFFORT="${PREDICTION_EFFORT:-xhigh}"
DSH_HOME="${DSH_HOME:-${HOME:-/home/agent}/.dsh-pred}"
mkdir -p "$LOG_DIR" "$DSH_HOME"

case "$PREDICTION_PROVIDER" in
  deepseek)
    : "${DEEPSEEK_API_KEY:?PREDICTION_PROVIDER=deepseek requires DEEPSEEK_API_KEY}"
    export DEEPSEEK_BASE_URL="https://api.deepseek.com/v1"
    ;;
  *)
    echo "deepseek-harness accepts only provider deepseek, got: $PREDICTION_PROVIDER" >&2
    exit 1
    ;;
esac

if ! python3 -c "import deepseek_harness" >/dev/null 2>&1; then
  echo "deepseek-harness SDK not found in this image" >&2
  exit 1
fi

export PRODUCT_DIR TASK_FILE DSH_HOME PREDICTION_MODEL PREDICTION_EFFORT
# The container is already isolated. danger-full-access skips the inner sandbox,
# which this kernel rejects, and sets approval to never.
export DSH_PERMISSION_MODE="${DSH_PERMISSION_MODE:-danger-full-access}"

cd "$PRODUCT_DIR"

echo "prediction harness: deepseek-harness"
echo "prediction provider: ${PREDICTION_PROVIDER}"
echo "prediction model: ${PREDICTION_MODEL}"
echo "prediction effort: ${PREDICTION_EFFORT} (mapped to dsh off/low/high/max)"
echo "prediction endpoint: ${DEEPSEEK_BASE_URL}"
echo "prediction dsh permission: ${DSH_PERMISSION_MODE}"

python3 - <<'PY'
import os
import sys
from pathlib import Path

from deepseek_harness import DeepSeekHarness

effort_map = {
    "off": "off",
    "low": "low",
    "medium": "high",
    "high": "high",
    "xhigh": "max",
    "max": "max",
}
raw = os.environ.get("PREDICTION_EFFORT", "xhigh").strip().lower()
reasoning = effort_map.get(raw, "high")
workspace = Path(os.environ["PRODUCT_DIR"]).resolve()
home = Path(os.environ["DSH_HOME"]).resolve()
prompt = Path(os.environ["TASK_FILE"]).read_text(encoding="utf-8")
kwargs = {
    "dsh_home": str(home),
    "cwd": str(workspace),
    "provider": "deepseek-official",
    "model": os.environ["PREDICTION_MODEL"],
    "reasoning_effort": reasoning,
    "profile": "sdk",
    "api_key": os.environ["DEEPSEEK_API_KEY"],
    "base_url": os.environ["DEEPSEEK_BASE_URL"],
}
print(f"dsh reasoning_effort: {reasoning}", flush=True)
try:
    with DeepSeekHarness(**kwargs) as harness:
        result = harness.run(prompt, session_id="pred")
except TypeError:
    kwargs.pop("profile", None)
    with DeepSeekHarness(**kwargs) as harness:
        result = harness.run(prompt, session_id="pred")
text = getattr(result, "final_response", None) or ""
reason = getattr(result, "finish_reason", None)
print(text)
if reason == "error":
    sys.exit("dsh finish_reason=error")
PY
