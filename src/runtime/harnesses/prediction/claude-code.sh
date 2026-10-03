#!/usr/bin/env bash
# Headless Claude Code, with fresh session state and no external tool integrations.
set -euo pipefail
PRODUCT_DIR="${PRODUCT_DIR:-/workspace/product}"
TASK_FILE="${TASK_FILE:-/workspace/task.md}"
LOG_DIR="${PREDICTION_LOG_DIR:-/workspace/output}"
PREDICTION_MODEL="${PREDICTION_MODEL:-claude-fable-5-1}"
PREDICTION_PROVIDER="${PREDICTION_PROVIDER:-anthropic}"
mkdir -p "$LOG_DIR"
umask 077
export CLAUDE_CONFIG_DIR
CLAUDE_CONFIG_DIR="$(mktemp -d "${HOME:?}/claude-run.XXXXXXXX")"
export DISABLE_AUTOUPDATER=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
export CLAUDE_CODE_SAFE_MODE=1 CLAUDE_CODE_DISABLE_AUTO_MEMORY=1
unset CLAUDE_CODE_SIMPLE CLAUDE_CODE_USE_BEDROCK CLAUDE_CODE_USE_VERTEX CLAUDE_CODE_USE_FOUNDRY

save_session() {
  local status=$?
  trap - EXIT
  # Retain every native session, including compaction and tool events, on failure too.
  if [ -d "$CLAUDE_CONFIG_DIR/projects" ]; then
    (cd "$CLAUDE_CONFIG_DIR" && find projects -type f -name '*.jsonl' -print0 |
      tar --null -T - -czf "$LOG_DIR/claude-sessions.tgz") || true
  fi
  exit "$status"
}
trap save_session EXIT

if [ "${PREDICTION_AUTH:-api}" = subscription ]; then
  [ "$PREDICTION_PROVIDER" = anthropic ] || { echo 'Claude OAuth requires provider=anthropic' >&2; exit 1; }
  : "${CLAUDE_CODE_OAUTH_TOKEN:?Claude OAuth credential missing}"
  unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL
else
  unset CLAUDE_CODE_OAUTH_TOKEN
  case "$PREDICTION_PROVIDER" in
    anthropic) : "${ANTHROPIC_API_KEY:?Anthropic API key missing}"; unset ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN ;;
    *) echo 'Claude Code only accepts official provider anthropic' >&2; exit 1 ;;
  esac
fi
command -v claude >/dev/null
claude --version > "$LOG_DIR/claude-version.txt"
# Fail closed if the image cannot disable injected integrations and context.
help="$(claude --help)"
for flag in --safe-mode --setting-sources --strict-mcp-config --tools --effort; do
  [[ "$help" == *"$flag"* ]] || { echo "Claude CLI lacks required isolation option: $flag" >&2; exit 1; }
done
cd "$PRODUCT_DIR"
cp "$TASK_FILE" "$LOG_DIR/prompt.md"
set +e
timed_command=()
if awk "BEGIN{exit !(${PREDICTION_WORK_MINUTES:-unlimited}+0 > 0)}" 2>/dev/null; then
  timed_command=(python3 /usr/local/bin/prediction-timed.py claude-code)
fi
"${timed_command[@]}" claude -p "$(cat "$TASK_FILE")" \
  --model "$PREDICTION_MODEL" --effort "${PREDICTION_EFFORT:-xhigh}" \
  --dangerously-skip-permissions --safe-mode --setting-sources '' \
  --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
  --tools 'Bash,Read,Edit,Write,Glob,Grep' \
  --disallowedTools 'WebFetch,WebSearch,Agent,Task,ToolSearch,mcp__*' \
  --output-format stream-json --verbose > "$LOG_DIR/trajectory.jsonl"
status=$?
set -e
[ "$status" -eq 0 ] || exit "$status"
# A successful process without a successful terminal result is not a prediction.
python3 - "$LOG_DIR/trajectory.jsonl" "$LOG_DIR/final.txt" "$LOG_DIR/timed-run.json" <<'PY'
import json, sys
from pathlib import Path
timing = Path(sys.argv[3])
if timing.is_file() and json.loads(timing.read_text()).get('status') == 'submission_timeout':
    Path(sys.argv[2]).write_text('Submission time limit reached. Collecting the existing working-tree diff; no successful final agent response.\n')
    raise SystemExit(0)
results = []
for line in Path(sys.argv[1]).read_text().splitlines():
    item = json.loads(line)
    if item.get('type') == 'result':
        results.append(item)
if not results or results[-1].get('is_error') or results[-1].get('subtype') != 'success':
    raise SystemExit('Claude did not produce a successful terminal result; see trajectory.jsonl')
Path(sys.argv[2]).write_text(str(results[-1].get('result', '')))
PY
