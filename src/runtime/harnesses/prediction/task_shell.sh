#!/usr/bin/env bash
# /workspace/task-shell <cmd>: docker exec into the pinned product container.
# The container name comes from a root-owned pin. Non-root calls re-exec through sudo.
# Each call is appended to status.shell.txt.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  if ! command -v sudo >/dev/null 2>&1; then
    echo "task-shell requires sudo in this image" >&2
    exit 1
  fi
  exec sudo -n "$0" "$@"
fi

PIN_FILE="/var/run/prediction-task-shell.env"
if [ ! -f "$PIN_FILE" ] || [ -L "$PIN_FILE" ]; then
  echo 'task-shell requires a trusted container pin' >&2
  exit 1
fi
if [ -f "$PIN_FILE" ]; then
  pin_owner="$(stat -c '%u' "$PIN_FILE" 2>/dev/null || echo x)"
  if [ "$pin_owner" != "0" ]; then
    echo "task-shell pin is not owned by root: $PIN_FILE" >&2
    exit 1
  fi
  PRODUCT_CONTAINER="$(sed -n '1p' "$PIN_FILE")"
  PIN_LOG_DIR="$(sed -n '2p' "$PIN_FILE")"
  [ -n "${PIN_LOG_DIR:-}" ] && LOG_DIR="$PIN_LOG_DIR"
fi

PRODUCT_CONTAINER="${PRODUCT_CONTAINER:?PRODUCT_CONTAINER is required}"
LOG_DIR="${LOG_DIR:-${PREDICTION_LOG_DIR:-/workspace/output}}"
SHELL_LOG="$LOG_DIR/status.shell.txt"

# The agent owns this directory. Never follow its log symlinks as root.
runuser -u agent -- mkdir -p "$LOG_DIR"
append_log() {
  runuser -u agent -- sh -c 'cat >> "$1"' sh "$SHELL_LOG"
}

{
  # Log the raw command with shell quoting.
  printf '[%s] ' "$(date -Iseconds)"
  printf '%q ' "$0" "$@"
  echo
} | append_log

set +e
# Run inside the product container, in the product directory.
docker exec "$PRODUCT_CONTAINER" bash -lc 'cd /workspace/product && exec "$@"' bash "$@"
exit_code=$?
set -e

echo "[exit=$exit_code]" | append_log

exit "$exit_code"
