#!/usr/bin/env bash
# Run the agent-browser evaluation container (--network host, BASE_URL=127.0.0.1:13200).
# Remaining args are the container command (run-workflows.py --phase ...).
set -euo pipefail

EVAL_IMAGE="${EVAL_IMAGE:?EVAL_IMAGE unset}"
AGENT_BROWSER_START_TIMEOUT="${AGENT_BROWSER_START_TIMEOUT:-120}"
RUNTIME="${RUNTIME:-/runtime}"

docker_args=(
  run --rm
  --network host
  --shm-size=1g
  -e "TIMEOUT_BrowserStartEvent=${AGENT_BROWSER_START_TIMEOUT}"
  -e "TIMEOUT_BrowserLaunchEvent=${AGENT_BROWSER_START_TIMEOUT}"
)

pass_env() {
  local name="$1"
  if [ -n "${!name:-}" ]; then
    docker_args+=(-e "${name}=${!name}")
  fi
}

if [ -n "${BASE_URL:-}" ]; then
  docker_args+=(
    -e "BASE_URL=${BASE_URL}"
    -e "no_proxy=127.0.0.1,localhost"
    -e "NO_PROXY=127.0.0.1,localhost"
  )
fi

for name in \
  EVALUATION_HARNESS EVALUATION_MODEL \
  AGENT_BROWSER_MODEL AGENT_BROWSER_PROVIDER AGENT_BROWSER_MAX_STEPS \
  AGENT_BROWSER_MAX_TOKENS AGENT_BROWSER_LLM_TIMEOUT AGENT_BROWSER_STEP_TIMEOUT \
  AGENT_BROWSER_TEMPERATURE AGENT_BROWSER_TOP_P AGENT_BROWSER_SEED \
  AGENT_BROWSER_RECORD AGENT_BROWSER_WORKFLOW_IDS AGENT_BROWSER_WORKFLOW_CATEGORIES \
  ANTHROPIC_API_KEY \
  OPENAI_API_KEY CODEX_API_KEY \
  EVALUATION_CODEX_AUTH EVALUATION_CODEX_PROVIDER \
  CODEX_REASONING_EFFORT CODEX_TIMEOUT_SECONDS
do
  pass_env "$name"
done

if [ -n "${WORKFLOWS_PATH:-}" ]; then
  docker_args+=(-v "${WORKFLOWS_PATH}:/evaluation/workflows.yaml:ro")
fi
docker_args+=(-v "${RUNTIME}/browser.py:/evaluation/run-workflows.py:ro")
docker_args+=(-v "${RUNTIME}/parse_results.py:/evaluation/parse_results.py:ro")
docker_args+=(-v "${RUNTIME}/harnesses/evaluation:/evaluation/harnesses:ro")

if [ "${EVALUATION_HARNESS:-browser-use}" = "codex" ]; then
  docker_args+=(-e "CODEX_EXECUTABLE=${CODEX_EXECUTABLE:-/opt/agent-bin/codex}")
  if [ "${EVALUATION_CODEX_AUTH:-subscription}" = "subscription" ]; then
    if [ -z "${CODEX_AUTH_FILE:-}" ] || [ ! -f "$CODEX_AUTH_FILE" ]; then
      echo "Codex subscription needs CODEX_AUTH_FILE" >&2
      exit 1
    fi
    docker_args+=(
      -v "${CODEX_AUTH_FILE}:/evaluation/secrets/codex-auth.json:ro"
      -e "CODEX_AUTH_FILE=/evaluation/secrets/codex-auth.json"
    )
  fi
fi

if [ -n "${EVAL_OUTPUT_DIR:-}" ]; then
  mkdir -p "$EVAL_OUTPUT_DIR"
  docker_args+=(-v "${EVAL_OUTPUT_DIR}:/evaluation/output")
fi
if [ -n "${EVAL_TEMPORARY_DIR:-}" ]; then
  mkdir -p "${EVAL_TEMPORARY_DIR}/setup"
  docker_args+=(-v "${EVAL_TEMPORARY_DIR}:/evaluation/temporary")
fi

exec docker "${docker_args[@]}" "$EVAL_IMAGE" "$@"
