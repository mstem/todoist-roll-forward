#!/usr/bin/env python3
"""
Todoist Roll-Forward
Moves tasks with a do date of today to tomorrow, preserving recurrence and leaving deadlines untouched.
Runs nightly so tomorrow's list is ready when you wake up.
"""

import json
import os
import subprocess
import datetime
import sys
import uuid

TOKEN = os.environ.get("TODOIST_API_TOKEN")
if not TOKEN:
    print("ERROR: TODOIST_API_TOKEN environment variable not set.", file=sys.stderr)
    sys.exit(1)

today = datetime.date.today().isoformat()
tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()

# /api/v1/tasks/filter with query=date:today targets the do date field only.
# query=due:today would also match tasks whose *deadline* is today — we never want those.
result = subprocess.run(
    ["curl", "-s", "-G",
     "https://api.todoist.com/api/v1/tasks/filter",
     "--data-urlencode", "query=date:today",
     "-H", f"Authorization: Bearer {TOKEN}"],
    capture_output=True, text=True
)

try:
    data = json.loads(result.stdout)
    tasks = data.get("results", [])
except json.JSONDecodeError:
    print(f"ERROR: Unexpected API response: {result.stdout[:200]}", file=sys.stderr)
    sys.exit(1)

if not tasks:
    print(f"No tasks with do date of today ({today}) — nothing to roll forward.")
    sys.exit(0)

# Build Sync API commands — one per task.
# We preserve the full due object (string, is_recurring, lang, timezone) and only update date.
# Using the Sync API item_update is required for recurring tasks; using due_date alone via the
# REST endpoint strips the recurrence string.
commands = []
for task in tasks:
    due = task.get("due") or {}
    new_due = {
        "date": tomorrow,
        "string": due.get("string", tomorrow),
        "is_recurring": due.get("is_recurring", False),
        "lang": due.get("lang", "en"),
    }
    if due.get("timezone"):
        new_due["timezone"] = due["timezone"]
    commands.append({
        "type": "item_update",
        "uuid": str(uuid.uuid4()),
        "args": {"id": task["id"], "due": new_due},
    })

r = subprocess.run(
    ["curl", "-s", "-X", "POST",
     "https://api.todoist.com/api/v1/sync",
     "-H", f"Authorization: Bearer {TOKEN}",
     "-H", "Content-Type: application/json",
     "-d", json.dumps({"commands": commands})],
    capture_output=True, text=True
)

try:
    sync_status = json.loads(r.stdout).get("sync_status", {})
except json.JSONDecodeError:
    print(f"ERROR: Unexpected sync response: {r.stdout[:200]}", file=sys.stderr)
    sys.exit(1)

updated = []
errors = []
for task, cmd in zip(tasks, commands):
    status = sync_status.get(cmd["uuid"], "unknown")
    if status == "ok":
        updated.append(task["content"])
    else:
        errors.append(f"{task['content']} (status: {status})")

print(f"Rolled forward {len(updated)} task(s) to {tomorrow}: {updated}")
if errors:
    print(f"Errors on {len(errors)} task(s): {errors}")
    sys.exit(1)
