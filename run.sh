#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"
set -a; source .env; set +a

TODAY=$(date +%Y-%m-%d)
LAST_RUN_FILE="$SCRIPT_DIR/.last-run"
LOG_FILE="$SCRIPT_DIR/roll-forward.log"

# Don't run twice on the same day
if [[ -f "$LAST_RUN_FILE" ]] && [[ "$(cat "$LAST_RUN_FILE")" == "$TODAY" ]]; then
  exit 0
fi

notify_failure() {
  osascript -e "display notification \"$1\" with title \"Todoist Roll-Forward\" subtitle \"Failed — tasks not moved\"" 2>/dev/null || true
}

if output=$(python3 rollforward.py 2>&1); then
  echo "[$TODAY] $output" >> "$LOG_FILE"
  echo "$TODAY" > "$LAST_RUN_FILE"
else
  echo "[$TODAY] ERROR: $output" >> "$LOG_FILE"
  notify_failure "$output"
  exit 1
fi

# Keep log to last 90 days
tail -n 90 "$LOG_FILE" > "$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"
