#!/usr/bin/env python3
"""
Todoist Roll-Forward
Moves overdue tasks (do date before today) forward to today, preserving recurrence
and leaving deadlines untouched. Runs nightly just after midnight Europe/Lisbon so
today's list is ready when you wake up.

Scheduling rules for the target day:
  * Work tasks (#work project + all sub-projects) are never scheduled onto a weekend:
    if today is Sat/Sun they roll to the upcoming Monday instead.
  * Unprioritised @weekend tasks are kept on weekends: on a weekday they roll to the
    upcoming Saturday rather than landing mid-week.

Pass --dry-run to print what would change without calling the Sync API.
"""

import json
import os
import subprocess
import datetime
import sys
import uuid
from zoneinfo import ZoneInfo

TOKEN = os.environ.get("TODOIST_API_TOKEN")
if not TOKEN:
    print("ERROR: TODOIST_API_TOKEN environment variable not set.", file=sys.stderr)
    sys.exit(1)

DRY_RUN = "--dry-run" in sys.argv

# Compute "today" in the *Todoist account's* timezone, NOT UTC and not a hardcoded zone.
# Todoist evaluates "today"/"overdue" server-side using the account timezone (currently
# Europe/Rome, UTC+1/+2), so the sweep must use the same zone or it lands a day off — the
# task shows as overdue even after "rolling" it. Two earlier bugs both reduced to this:
# datetime.date.today() used UTC, and the nightly GitHub Actions run is often delayed ~1h
# past midnight UTC. Fetching the account zone keeps the target day correct regardless of
# firing time, and self-corrects if the account timezone ever changes.
def fetch_account_timezone():
    try:
        r = subprocess.run(
            ["curl", "-s", "-X", "POST", "https://api.todoist.com/api/v1/sync",
             "-H", f"Authorization: Bearer {TOKEN}",
             "--data-urlencode", "sync_token=*",
             "--data-urlencode", 'resource_types=["user"]'],
            capture_output=True, text=True)
        return ZoneInfo(json.loads(r.stdout)["user"]["tz_info"]["timezone"])
    except Exception as e:
        print(f"WARNING: could not read account timezone ({e}); falling back to Europe/Lisbon.",
              file=sys.stderr)
        return ZoneInfo("Europe/Lisbon")

TZ = fetch_account_timezone()
today = datetime.datetime.now(TZ).date()
today_iso = today.isoformat()

# Only sweep tasks overdue within this many days. Anything older is deliberately left
# in place to be triaged/archived by hand, so a long-neglected backlog never buries
# today's list. In nightly steady state nothing should be more than a day or two behind.
ROLL_BACK_DAYS = int(os.environ.get("ROLL_BACK_DAYS", "30"))
cutoff_iso = (today - datetime.timedelta(days=ROLL_BACK_DAYS)).isoformat()

def upcoming(weekday, ref=None):
    """The given weekday (Mon=0 .. Sun=6) falling on or after `ref` (default today)."""
    ref = ref or today
    return ref + datetime.timedelta(days=(weekday - ref.weekday()) % 7)

upcoming_monday = upcoming(0).isoformat()
upcoming_saturday = upcoming(5).isoformat()
today_is_weekend = today.weekday() >= 5  # Sat (5) or Sun (6)

def fetch_projects():
    projects = []
    cursor = None
    while True:
        args = ["curl", "-s", "-G", "https://api.todoist.com/api/v1/projects",
                "-H", f"Authorization: Bearer {TOKEN}"]
        if cursor:
            args += ["--data-urlencode", f"cursor={cursor}"]
        result = subprocess.run(args, capture_output=True, text=True)
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            print(f"ERROR: Could not fetch projects: {result.stdout[:200]}", file=sys.stderr)
            sys.exit(1)
        page = data.get("results", data) if isinstance(data, dict) else data
        if isinstance(page, list):
            projects.extend(page)
        cursor = data.get("next_cursor") if isinstance(data, dict) else None
        if not cursor:
            break
    return projects

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

# Work tasks only need special handling when today is a weekend (bump them to Monday),
# so only pay the cost of fetching the project tree on those days.
work_project_ids = collect_work_project_ids(fetch_projects()) if today_is_weekend else set()
if today_is_weekend:
    print(f"Weekend ({today_iso}): {len(work_project_ids)} work project(s) found; "
          f"work tasks roll to {upcoming_monday}, other tasks stay on the weekend.")

# Fetch all overdue tasks (do date strictly before today). Using "overdue" instead of a
# single exact date ensures nothing is ever stranded: a task added late for its own day,
# or one that slipped more than a day behind, still gets caught and pulled forward.
# "overdue" keys off the due date only, so deadline-only tasks are left untouched.
def fetch_tasks_page(cursor=None):
    args = ["curl", "-s", "-G",
            "https://api.todoist.com/api/v1/tasks/filter",
            "--data-urlencode", "query=overdue",
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

# Drop tasks older than the look-back window (the due date may carry a time component,
# e.g. "2026-01-07T19:00:00Z", so compare on the leading YYYY-MM-DD only).
def due_date_of(task):
    return ((task.get("due") or {}).get("date") or "")[:10]

total_overdue = len(tasks)
tasks = [t for t in tasks if due_date_of(t) >= cutoff_iso]
skipped = total_overdue - len(tasks)
if skipped:
    print(f"Skipping {skipped} task(s) overdue before {cutoff_iso} "
          f"(older than ROLL_BACK_DAYS={ROLL_BACK_DAYS}); left in place for manual triage.")

if not tasks:
    print("No overdue tasks within the look-back window — nothing to roll forward.")
    sys.exit(0)

print(f"Found {len(tasks)} overdue task(s) to roll forward to {today_iso}.")

# Build Sync API commands — one per task.
# We preserve the full due object (string, is_recurring, lang, timezone) and only update
# the date. Using the Sync API item_update is required for recurring tasks; setting a
# due_date alone via the REST endpoint would strip the recurrence string.
def target_for(task):
    has_no_priority = task.get("priority", 1) == 1
    is_work_task = task.get("project_id") in work_project_ids
    is_weekend_tagged = "weekend" in task.get("labels", [])
    if is_work_task and today_is_weekend:
        return upcoming_monday           # keep work off the weekend
    if is_weekend_tagged and has_no_priority and not today_is_weekend:
        return upcoming_saturday         # keep @weekend tasks on the weekend
    return today_iso

commands = []
for task in tasks:
    due = task.get("due") or {}
    target_date = target_for(task)
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

if DRY_RUN:
    from collections import Counter
    by_target = Counter(c["args"]["due"]["date"] for c in commands)
    print(f"[dry-run] would move {len(commands)} task(s):")
    for tgt, n in sorted(by_target.items()):
        print(f"[dry-run]   -> {tgt}: {n} task(s)")
    for task, cmd in zip(tasks, commands):
        old = (task.get("due") or {}).get("date")
        print(f"[dry-run]   {old} -> {cmd['args']['due']['date']}  {task['content'][:60]!r}")
    sys.exit(0)

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
