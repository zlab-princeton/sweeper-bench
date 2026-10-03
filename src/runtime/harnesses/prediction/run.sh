#!/usr/bin/env bash
# Prediction entry, mounted by pred_container.sh as run-agent-prediction.sh.
# Run the selected harness, then write the patch.
set -euo pipefail

# Product repo, task, and output are mounted at these paths.
PRODUCT_DIR="${PRODUCT_DIR:-/workspace/product}"
OUTPUT_DIR="${OUTPUT_DIR:-/workspace/output}"
LOG_DIR="${PREDICTION_LOG_DIR:-$OUTPUT_DIR}"
PATCH_FILE="${PREDICTION_PATCH_FILE:-$OUTPUT_DIR/prediction.patch}"
TASK_FILE="${TASK_FILE:-/workspace/task.md}"
AGENT_USER=agent
PREDICTION_HARNESS="${PREDICTION_HARNESS:-codex}"
PREDICTION_PROVIDER="${PREDICTION_PROVIDER:-openai}"
PREDICTION_MODEL="${PREDICTION_MODEL:-gpt-6-astra}"
HARNESS_SCRIPT="${PREDICTION_HARNESS_SCRIPT:-/usr/local/bin/prediction-harness-${PREDICTION_HARNESS}}"

# One path set for every harness.
export PRODUCT_DIR OUTPUT_DIR LOG_DIR TASK_FILE PATCH_FILE HARNESS_SCRIPT

mkdir -p "$OUTPUT_DIR" "$LOG_DIR"
cd "$PRODUCT_DIR"

# The mounted repo may be owned by another user.
git config --global --add safe.directory "$PRODUCT_DIR"

# Binary: /usr/local/bin/prediction-harness-${PREDICTION_HARNESS}
if [ ! -x "$HARNESS_SCRIPT" ]; then
  echo "prediction harness not found: $HARNESS_SCRIPT" >&2
  echo "PREDICTION_HARNESS=${PREDICTION_HARNESS}" >&2
  exit 1
fi

# docker.sock stays with root. The agent enters the product only through task-shell.
# Root pins the product container name so the agent cannot retarget it.
RUN_AS_AGENT=0
if [ "$(id -u)" -eq 0 ] && getent passwd "$AGENT_USER" >/dev/null; then
  RUN_AS_AGENT=1
  if [ -n "${PRODUCT_CONTAINER:-}" ]; then
    PIN_FILE="/var/run/prediction-task-shell.env"
    printf '%s\n%s\n' "$PRODUCT_CONTAINER" "$LOG_DIR" >"$PIN_FILE"
    chmod 644 "$PIN_FILE"
  fi
  if command -v sudo >/dev/null 2>&1 && [ -x /workspace/task-shell ]; then
    printf 'agent ALL=(root) NOPASSWD: /workspace/task-shell\n' > /etc/sudoers.d/prediction-task-shell
    chmod 440 /etc/sudoers.d/prediction-task-shell
  fi
  chown -R "$AGENT_USER:$AGENT_USER" "$PRODUCT_DIR" "$OUTPUT_DIR" "$LOG_DIR"
fi

if [ "$PREDICTION_HARNESS" = claude-code ] && [ "$RUN_AS_AGENT" != 1 ]; then
  echo 'Claude prediction requires the unprivileged agent account' >&2
  exit 1
fi

# status.shell.txt lists commands run in the product container.
SHELL_LOG="$LOG_DIR/status.shell.txt"
{
  echo "# Commands run in the product container through /workspace/task-shell."
  echo "# Commands that do not use task-shell are omitted."
  echo "# started $(date -Iseconds)"
  echo "# PREDICTION_HARNESS=${PREDICTION_HARNESS}"
  echo "# PREDICTION_PROVIDER=${PREDICTION_PROVIDER}"
  echo "# PREDICTION_MODEL=${PREDICTION_MODEL}"
  echo "# ANTHROPIC_API=official"
} >"$SHELL_LOG"
if [ "$RUN_AS_AGENT" = "1" ]; then
  chown "$AGENT_USER:$AGENT_USER" "$SHELL_LOG"
fi

# harness.meta.json records harness, provider, and model. Keys are present/absent only.
python3 - <<'PY' >"$LOG_DIR/harness.meta.json"
import json
import os

secret_names = [
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "CURSOR_API_KEY",
    "DEEPSEEK_API_KEY",
    "KIMI_API_KEY",
    "GEMINI_API_KEY",
    "META_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
]
meta = {
    "harness": os.environ.get("PREDICTION_HARNESS", ""),
    "provider": os.environ.get("PREDICTION_PROVIDER", ""),
    "model": os.environ.get("PREDICTION_MODEL", ""),
    "level": os.environ.get("PREDICTION_LEVEL", ""),
    "endpoints": {
        "anthropic": "api.anthropic.com",
        "openai": "api.openai.com",
    },
    "secrets_present": {name: bool(os.environ.get(name)) for name in secret_names},
}
print(json.dumps(meta, indent=2, ensure_ascii=False))
PY
if [ "$RUN_AS_AGENT" = "1" ]; then
  chown "$AGENT_USER:$AGENT_USER" "$LOG_DIR/harness.meta.json"
fi

# Git status before and after the harness.
git status --short >"$LOG_DIR/status.before.txt"

STDOUT_LOG="$LOG_DIR/harness.stdout.txt"
STDERR_LOG="$LOG_DIR/harness.stderr.txt"

# Run the agent. Keep stdout and stderr files on failure.
set +e
if [ "$RUN_AS_AGENT" = "1" ]; then
  export HARNESS_SCRIPT
  su -m -s /bin/bash "$AGENT_USER" -c 'export HOME=/home/agent && cd "$PRODUCT_DIR" && "$HARNESS_SCRIPT"' >"$STDOUT_LOG" 2>"$STDERR_LOG"
  harness_exit=$?
else
  "$HARNESS_SCRIPT" >"$STDOUT_LOG" 2>"$STDERR_LOG"
  harness_exit=$?
fi
set -e

git status --short >"$LOG_DIR/status.after.txt"
if [ "$RUN_AS_AGENT" = "1" ]; then
  chown "$AGENT_USER:$AGENT_USER" "$STDOUT_LOG" "$STDERR_LOG" "$LOG_DIR/status.before.txt" "$LOG_DIR/status.after.txt" 2>/dev/null || true
fi

if [ "$harness_exit" -ne 0 ]; then
  echo "prediction harness failed: ${PREDICTION_HARNESS} (exit=$harness_exit)" >&2
  echo "stdout: $STDOUT_LOG" >&2
  echo "stderr: $STDERR_LOG" >&2
  exit "$harness_exit"
fi

# Tracked edits via git diff. New source via ls-files, minus runtime junk.
# Intent-to-add so new files appear in the same binary patch.
keep_untracked() {
  local p="$1"
  [ -f "$p" ] || return 1
  local sz
  sz="$(wc -c <"$p")"
  [ "$sz" -gt 0 ] || return 1
  [ "$sz" -le 1048576 ] || return 1
  case "$p" in
    router.php|*/router.php|*-router.php|*/*-router.php) return 1 ;;
    .product-deps-linked|*/.product-deps-linked|.product-deps*|*/.product-deps*) return 1 ;;
  esac
  case "/$p/" in
    */node_modules/*|*/vendor/*|*/dist/*|*/build/*|*/coverage/*|*/__pycache__/*|*/templates_c/*|*/.cache/*) return 1 ;;
  esac
  return 0
}

UNTRACKED_LOG="$LOG_DIR/status.untracked.txt"
: >"$UNTRACKED_LOG"
while IFS= read -r -d '' path; do
  if keep_untracked "$path"; then
    echo "keep $path" >>"$UNTRACKED_LOG"
    git add -N -- "$path"
  else
    echo "skip $path" >>"$UNTRACKED_LOG"
  fi
done < <(git ls-files -z --others --exclude-standard)

git diff --binary >"$PATCH_FILE"

# An empty diff cannot be evaluated.
if [ ! -s "$PATCH_FILE" ] || ! grep -qE '^diff --git ' "$PATCH_FILE"; then
  echo "patch is empty or has no code change: $PATCH_FILE" >&2
  exit 1
fi

{
  echo "# finished $(date -Iseconds)"
} >>"$SHELL_LOG"

echo "prediction patch: $PATCH_FILE"
echo "shell log: $SHELL_LOG"
echo "Harness stdout: $STDOUT_LOG"
echo "Harness stderr: $STDERR_LOG"
