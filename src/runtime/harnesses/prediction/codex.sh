#!/usr/bin/env bash
# Codex adapter. Reads /workspace/product and /workspace/task.md. The outer script writes the patch.
set -euo pipefail

# Paths come from run-agent-prediction.sh.
PRODUCT_DIR="${PRODUCT_DIR:-/workspace/product}"
TASK_FILE="${TASK_FILE:-/workspace/task.md}"
PREDICTION_PROVIDER="${PREDICTION_PROVIDER:-openai}"
PREDICTION_MODEL="${PREDICTION_MODEL:?PREDICTION_MODEL is required}"

if ! command -v codex >/dev/null 2>&1; then
  echo "codex CLI not found in this image" >&2
  exit 1
fi

write_codex_provider_config() {
  local provider_name="prediction-provider"
  local base_url=""
  local env_key=""

  # Official OpenAI only. CODEX_API_KEY uses the CLI default endpoint.
  case "$PREDICTION_PROVIDER" in
    openai)
      if [ -n "${OPENAI_API_KEY:-}" ]; then
        base_url="https://api.openai.com/v1"
        env_key="OPENAI_API_KEY"
      elif [ -n "${CODEX_API_KEY:-}" ]; then
        return 0
      else
        echo "codex provider=openai requires OPENAI_API_KEY or CODEX_API_KEY" >&2
        exit 1
      fi
      ;;
    *)
      echo "codex accepts only provider openai, got: $PREDICTION_PROVIDER" >&2
      exit 1
      ;;
  esac

  if [ -z "$base_url" ] || [ -z "$env_key" ]; then
    return 0
  fi

  mkdir -p "$HOME/.codex"
  cat >"$HOME/.codex/config.toml" <<EOF
model = "$PREDICTION_MODEL"
model_provider = "$provider_name"
sandbox_mode = "danger-full-access"
approval_policy = "never"
web_search = "disabled"

[features]
apps = false
multi_agent = false

[apps._default]
enabled = false

[apps.github]
enabled = false

[model_providers.$provider_name]
name = "$PREDICTION_PROVIDER"
base_url = "$base_url"
env_key = "$env_key"
wire_api = "responses"
EOF
}

cd "$PRODUCT_DIR"
if [ "${PREDICTION_AUTH:-api}" = "subscription" ]; then
  export CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
  python3 - <<'AUTH'
import json, os
from pathlib import Path
home = Path(os.environ["CODEX_HOME"])
home.mkdir(parents=True, exist_ok=True)
auth = json.loads(os.environ["CODEX_AUTH_JSON"])
if not auth.get("tokens", {}).get("access_token"):
    raise SystemExit("Subscription credentials have no access token")
p = home / "auth.json"
p.touch(mode=0o600, exist_ok=True)
p.write_text(json.dumps(auth))
p.chmod(0o600)
AUTH
  unset CODEX_AUTH_JSON OPENAI_API_KEY CODEX_API_KEY
  auth_args=(--ignore-user-config)
else
  unset CODEX_AUTH_JSON
  write_codex_provider_config
  auth_args=()
fi

echo "Codex version:"
codex --version || true
echo "prediction harness: codex"
echo "prediction provider: ${PREDICTION_PROVIDER}"
echo "prediction model: ${PREDICTION_MODEL}"
CODEX_EFFORT="${PREDICTION_EFFORT:-xhigh}"
echo "prediction effort: ${PREDICTION_EFFORT:-xhigh} (codex=${CODEX_EFFORT})"

# Keep the native session trace as well as stdout/stderr, never the auth file.
# EXIT also runs on agent failure so partial traces survive for diagnosis.
if [ -n "${LOG_DIR:-}" ]; then
  cp "$TASK_FILE" "$LOG_DIR/prompt.txt"
  collect_codex_sessions() {
    python3 - <<'TRACE'
import os, tarfile
from pathlib import Path
home = Path(os.environ.get("CODEX_HOME") or str(Path.home() / ".codex"))
sessions = home / "sessions"
with tarfile.open(Path(os.environ["LOG_DIR"]) / "native-sessions.tgz", "w:gz") as archive:
    for path in sorted(sessions.rglob("*.jsonl")):
        if path.is_file() and not path.is_symlink():
            archive.add(path, arcname=str(path.relative_to(sessions)))
TRACE
  }
  trap 'collect_codex_sessions || true' EXIT
fi

# Codex reads env_key from ~/.codex/config.toml. Do not pass --ignore-user-config.
export CODEX_API_KEY OPENAI_API_KEY
# The container is already isolated. Disable Codex's bwrap sandbox; this kernel rejects it.
# Also disable Apps, the GitHub connector, and web search.
timed_command=()
if awk "BEGIN{exit !(${PREDICTION_WORK_MINUTES:-unlimited}+0 > 0)}" 2>/dev/null; then
  timed_command=(python3 /usr/local/bin/prediction-timed.py codex)
fi
"${timed_command[@]}" codex exec "${auth_args[@]}" \
  --skip-git-repo-check \
  --dangerously-bypass-approvals-and-sandbox \
  --model "$PREDICTION_MODEL" \
  --config "model_reasoning_effort=\"${CODEX_EFFORT}\"" \
  --config 'web_search="disabled"' \
  --config 'features.apps=false' \
  --config 'features.multi_agent=false' \
  --config 'apps._default.enabled=false' \
  --config 'apps.github.enabled=false' \
  - <"$TASK_FILE"
