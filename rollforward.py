#!/usr/bin/env python3
"""
Todoist Roll-Forward
Moves tasks with a do date of yesterday to today, leaving deadlines untouched.
"""

import json
import os
import subprocess
import datetime
import sys

TOKEN = os.environ.get("TODOIST_API_TOKEN")
if not TOKEN:
    print("ERROR: TODOIST_API_TOKEN environment variable not set.", file=sys.stderr)
    sys.exit(1)

today = datetime.date.today().isoformat()
yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()

# date:yesterday targets the do date / scheduled date field only.
# due:yesterday also matches tasks whose *deadline* is yesterday — we never want those.
result = subprocess.run(
    ["curl", "-s", "-X", "GET",
     "https://api.todoist.com/rest/v2/tasks?filter=date%3Ayesterday",
     "-H", f"Authorization: Bearer {TOKEN}"],
    capture_output=True, text=True
)

try:
    tasks = json.loads(result.stdout)
except json.JSONDecodeError:
    print(f"ERROR: Unexpected API response: {result.stdout[:200]}", file=sys.stderr)
    sys.exit(1)

if not tasks:
    print(f"No tasks with do date of yesterday ({yesterday}) — nothing to roll forward.")
    sys.exit(0)

updated = []
errors = []

for task in tasks:
    task_id = task["id"]
    name = task["content"]

    r = subprocess.run(
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
         "-X", "POST", f"https://api.todoist.com/rest/v2/tasks/{task_id}",
         "-H", f"Authorization: Bearer {TOKEN}",
         "-H", "Content-Type: application/json",
         "-d", json.dumps({"due_date": today})],
        capture_output=True, text=True
    )

    if r.stdout.strip() == "204":
        updated.append(name)
    else:
        errors.append(f"{name} (HTTP {r.stdout.strip()})")

print(f"Rolled forward {len(updated)} task(s) to {today}: {updated}")
if errors:
    print(f"Errors on {len(errors)} task(s): {errors}")
    sys.exit(1)
