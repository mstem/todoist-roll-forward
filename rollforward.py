#!/usr/bin/env python3
"""
Todoist Roll-Forward
Moves tasks with a do date of yesterday to tomorrow, preserving recurrence and leaving deadlines untouched.
Runs nightly at 12:02 AM WET so tomorrow's list is ready when you wake up.
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

# At 23:02 UTC (= 12:02 AM WET/WEST), UTC "today" equals WET "yesterday".
# Using the explicit date avoids pulling in the entire overdue backlog.
wet_yesterday_date = datetime.date.today()
wet_yesterday = wet_yesterday_date.isoformat()
wet_today = (wet_yesterday_date + datetime.timedelta(days=1)).isoformat()
wet_monday = (wet_yesterday_date + datetime.timedelta(days=3)).isoformat()
wet_saturday = (wet_yesterday_date + datetime.timedelta(days=6)).isoformat()

is_friday_rollover = (wet_yesterday_date.weekday() == 4)  # 4 = Friday
is_sunday_rollover = (wet_yesterday_date.weekday() == 6)  # 6 = Sunday

def fetch_projects():
    result = subprocess.run(
        ["curl", "-s", "https://api.todoist.com/api/v1/projects",
         "-H", f"Authorization: Bearer {TOKEN}"],
        capture_output=True, text=True
    )
    try:
        data = json.loads(result.stdout)
        return data.get("results", data) if isinstance(data, dict) else data
    except json.JSONDecodeError:
        print(f"ERROR: Could not fetch projects: {result.stdout[:200]}", file=sys.stderr)
        sys.exit(1)

def collect_work_project_ids(projects):
    """Return IDs of the #work project and all its sub-projects (any depth)."""
    root_ids = {p["id"] for p in projects if p.get("name", "").lower() == "work"}
    if not root_ids:
        return set()
    all_ids = set(root_ids)
    frontier = set(root_ids)
    while frontier:
        children = {p["id"] for p in projects if p.get("parent_id") in frontier}
        new_children = children - all_ids
        all_ids |= new_children
        frontier = new_children
    return all_ids

work_project_ids = collect_work_project_ids(fetch_projects()) if is_friday_rollover else set()
if is_friday_rollover:
    print(f"Friday rollover: work project IDs found: {len(work_project_ids)}")
if is_sunday_rollover:
    print(f"Sunday rollover: @weekend tasks with no priority will roll to {wet_saturday}.")

# Fetch all pages of tasks scheduled for exactly yesterday (WET).
# Using an explicit date instead of "yesterday" ensures we only get tasks
# assigned for that specific day, not the entire overdue backlog.
# Using "date:" (not "due:") avoids matching tasks whose only deadline falls on that day.
def fetch_tasks_page(cursor=None):
    args = ["curl", "-s", "-G",
            "https://api.todoist.com/api/v1/tasks/filter",
            "--data-urlencode", f"query=date:{wet_yesterday}",
            "-H", f"Authorization: Bearer {TOKEN}"]
    if cursor:
        args += ["--data-urlencode", f"cursor={cursor}"]
    result = subprocess.run(args, capture_output=True, text=True)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"ERROR: Unexpected API response: {result.stdout[:200]}", file=sys.stderr)
        sys.exit(1)

tasks = []
cursor = None
while True:
    data = fetch_tasks_page(cursor)
    tasks.extend(data.get("results", []))
    cursor = data.get("next_cursor")
    if not cursor:
        break

if not tasks:
    print(f"No tasks scheduled for {wet_yesterday} — nothing to roll forward.")
    sys.exit(0)

print(f"Found {len(tasks)} task(s) scheduled for {wet_yesterday} to roll forward.")

# Build Sync API commands — one per task.
# We preserve the full due object (string, is_recurring, lang, timezone) and only update date.
# Using the Sync API item_update is required for recurring tasks; using due_date alone via the
# REST endpoint strips the recurrence string.
commands = []
for task in tasks:
    due = task.get("due") or {}
    has_no_priority = task.get("priority", 1) == 1
    is_work_task = task.get("project_id") in work_project_ids
    is_weekend_tagged = "weekend" in task.get("labels", [])
    if is_friday_rollover and is_work_task and has_no_priority:
        target_date = wet_monday
    elif is_sunday_rollover and is_weekend_tagged and has_no_priority:
        target_date = wet_saturday
    else:
        target_date = wet_today
    new_due = {
        "date": target_date,
        "string": due.get("string", target_date),
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

BATCH_SIZE = 100
sync_status = {}
for i in range(0, len(commands), BATCH_SIZE):
    batch = commands[i:i + BATCH_SIZE]
    r = subprocess.run(
        ["curl", "-s", "-X", "POST",
         "https://api.todoist.com/api/v1/sync",
         "-H", f"Authorization: Bearer {TOKEN}",
         "-H", "Content-Type: application/json",
         "-d", json.dumps({"commands": batch})],
        capture_output=True, text=True
    )
    try:
        sync_status.update(json.loads(r.stdout).get("sync_status", {}))
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

print(f"Rolled forward {len(updated)} task(s): {updated}")
if errors:
    print(f"Errors on {len(errors)} task(s): {errors}")
    sys.exit(1)
