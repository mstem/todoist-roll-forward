#!/bin/bash
# Nightly Todoist roll-forward, fired at 01:15 UTC from root's crontab. This is the
# primary runner: GitHub Actions delivered its scheduled runs 1.7-7.5h late through
# August 2026 and eventually dropped one, stranding a day of tasks, while a host cron
# fires on time and has no quota or inactivity rules attached to it.
#
# The Actions workflow in the repo is still enabled and still firing (01:37 and 08:23
# UTC), so the sweep runs from two places. That is duplicated work rather than a
# conflict: whichever fires first rolls the overdue tasks and backlogs anything past
# ROLLOVER_LIMIT, and the later one then finds nothing overdue and skips the tasks
# already carrying the label. Disable the workflow if the duplication starts to matter.
#
# What the sweep now does, beyond moving overdue tasks to today: a task whose push
# count (Todoist's postponed_count) passes ROLLOVER_LIMIT stops being moved and is
# backlogged instead — @backlog label on, due date cleared — which takes it off the
# today list and onto the Backlog page in todoist-triage. Prioritised and recurring
# tasks are exempt.
#
# This script is deployed ONE LEVEL ABOVE the clone, because it owns both the clone and
# the token:
#
#   /opt/todoist-roll-forward/
#     run.sh    this script, mode 700, copied from repo/deploy/run.sh
#     .env      TODOIST_API_TOKEN, mode 600, never in git
#     repo/     clone of github.com/mstem/todoist-roll-forward, pulled --ff-only each run
#     roll-forward.log, .last-success
#
# rollforward.py updates itself through the pull; this wrapper does not. After changing
# deploy/run.sh, copy it up by hand:
#
#   ssh root@91.98.23.69 'cp /opt/todoist-roll-forward/repo/deploy/run.sh \
#     /opt/todoist-roll-forward/run.sh && chmod 700 /opt/todoist-roll-forward/run.sh'
#
# Installed by root's crontab as:
#   15 1 * * * /opt/todoist-roll-forward/run.sh >> /root/.claude/logs/todoist-roll-forward-cron.log 2>&1
set -uo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
set -a; source "$DIR/.env"; set +a

LOG="$DIR/roll-forward.log"
STAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)

log() { printf '[%s] %s\n' "$STAMP" "$*" >> "$LOG"; }

# Pick up script changes, but never let an unreachable remote stop the sweep:
# the existing checkout is good enough to run tonight.
if ! pull=$(git -C repo pull --ff-only -q 2>&1); then
  log "WARNING: git pull failed, running the existing checkout: $pull"
  logger -t todoist-roll-forward -p user.warning "git pull failed: $(printf '%s' "$pull" | head -c 200)"
fi

if output=$(python3 repo/rollforward.py 2>&1); then
  log "OK ($(git -C repo rev-parse --short HEAD))"
  printf '%s\n' "$output" >> "$LOG"
  date -u +%Y-%m-%d > "$DIR/.last-success"
  logger -t todoist-roll-forward "ok: $(printf '%s' "$output" | head -c 200)"
  status=0
else
  log "FAILED ($(git -C repo rev-parse --short HEAD))"
  printf '%s\n' "$output" >> "$LOG"
  logger -t todoist-roll-forward -p user.err "FAILED: $(printf '%s' "$output" | head -c 500)"
  status=1
fi

# Cap the log; each run can print a long task list.
tail -n 2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
exit $status
