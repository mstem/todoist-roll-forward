#!/usr/bin/env python3
"""
Todoist Roll-Forward
Moves overdue tasks (do date before today) forward to today, preserving recurrence
and leaving deadlines untouched. Runs nightly just after midnight Europe/Lisbon so
today's list is ready when you wake up.

Only tasks with no assignee, or assigned to this account, are touched: a filter query
also returns tasks a collaborator has been assigned in a shared project, and those are
theirs to reschedule. A time of day on the due date is carried across the move.

Scheduling rules for the target day:
  * Work tasks (#work project + all sub-projects) are never scheduled onto a weekend:
    if today is Sat/Sun they roll to the upcoming Monday instead.
  * @weekend tasks are kept on weekends: on a weekday they roll to the upcoming
    Saturday rather than landing mid-week. The label wins regardless of priority, the
    same way the #work rule above does.
  * A task pushed more than ROLLOVER_LIMIT times (Todoist's own postponed_count) stops
    being rolled: it gets the @backlog label and its due date cleared, which takes it off
    the today list and onto the Backlog page in todoist-triage. That is the same label
    todoist-triage applies when a task is swiped to Backlog by hand, so both routes off
    the today list land in the same place. Prioritised and recurring tasks are exempt.

The label and project names the rules key off are checked against the account on every
run. A rename in the Todoist app would otherwise switch a rule off in silence, so a miss
is reported and fails the run once the roll itself has finished.

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
def fetch_account():
    """Returns (timezone, own user id). The user id identifies which tasks are ours."""
    try:
        r = subprocess.run(
            ["curl", "-s", "-X", "POST", "https://api.todoist.com/api/v1/sync",
             "-H", f"Authorization: Bearer {TOKEN}",
             "--data-urlencode", "sync_token=*",
             "--data-urlencode", 'resource_types=["user"]'],
            capture_output=True, text=True)
        user = json.loads(r.stdout)["user"]
        return ZoneInfo(user["tz_info"]["timezone"]), str(user["id"])
    except Exception as e:
        print(f"WARNING: could not read account details ({e}); falling back to Europe/Lisbon, "
              "and every assigned task will be left alone.", file=sys.stderr)
        return ZoneInfo("Europe/Lisbon"), None

TZ, MY_UID = fetch_account()
today = datetime.datetime.now(TZ).date()
today_iso = today.isoformat()

# Only sweep tasks overdue within this many days. Anything older is deliberately left
# in place to be triaged/archived by hand, so a long-neglected backlog never buries
# today's list. In nightly steady state nothing should be more than a day or two behind.
ROLL_BACK_DAYS = int(os.environ.get("ROLL_BACK_DAYS", "30"))
cutoff_iso = (today - datetime.timedelta(days=ROLL_BACK_DAYS)).isoformat()

# A task that has been pushed forward this many times is not a "today" task any more,
# it is backlog. Past the limit it gets tagged and its due date dropped instead of being
# moved again, so it leaves both this sweep and the Do-Today deck in todoist-triage, and
# surfaces on that app's Backlog page instead.
#
# The count comes from Todoist's own `postponed_count` on each task, so there is no state
# to keep between runs — which matters because this runs on a fresh GitHub Actions runner
# every night with nothing writable to carry a counter in. It counts every postponement,
# including ones made by hand in the Todoist app, not just this script's.
ROLLOVER_LIMIT = int(os.environ.get("ROLLOVER_LIMIT", "21"))
BACKLOG_LABEL = os.environ.get("BACKLOG_LABEL", "backlog")
WEEKEND_LABEL = os.environ.get("WEEKEND_LABEL", "weekend")
WORK_PROJECT = os.environ.get("WORK_PROJECT", "work")

# Every scheduling rule here keys off a name typed into Todoist, so renaming the label or
# the project in the app turns the matching rule off. Nothing errors when that happens:
# the sweep keeps running and keeps reporting success while quietly scheduling weekend
# tasks onto Tuesdays. So the names are checked against the account up front, matched
# without case, and a miss is reported loudly and fails the run at the end (after the
# roll, so a typo in a label never strands a day of tasks).
config_errors = []

def has_label(task, name):
    return any(l.lower() == name.lower() for l in task.get("labels", []))

def check_labels_exist():
    r = subprocess.run(["curl", "-s", "-G", "https://api.todoist.com/api/v1/labels",
                        "-H", f"Authorization: Bearer {TOKEN}"], capture_output=True, text=True)
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        config_errors.append(f"could not read the label list: {r.stdout[:120]}")
        return
    names = {l.get("name", "").lower() for l in (data.get("results", data) or [])}
    for label in (WEEKEND_LABEL, BACKLOG_LABEL):
        if label.lower() not in names:
            config_errors.append(
                f"no label named @{label} exists in Todoist, so the rule that depends on "
                f"it does nothing (rename it back, or set the matching env var)")

check_labels_exist()

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
    root_ids = {p["id"] for p in projects if p.get("name", "").lower() == WORK_PROJECT.lower()}
    if not root_ids:
        config_errors.append(
            f"no project named #{WORK_PROJECT} exists, so work tasks were not kept off "
            f"the weekend (rename it back, or set WORK_PROJECT)")
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
def fetch_tasks_page(query, cursor=None):
    args = ["curl", "-s", "-G",
            "https://api.todoist.com/api/v1/tasks/filter",
            "--data-urlencode", f"query={query}",
            "-H", f"Authorization: Bearer {TOKEN}"]
    if cursor:
        args += ["--data-urlencode", f"cursor={cursor}"]
    result = subprocess.run(args, capture_output=True, text=True)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"ERROR: Unexpected API response: {result.stdout[:200]}", file=sys.stderr)
        sys.exit(1)

def fetch_tasks(query):
    out = []
    cursor = None
    while True:
        data = fetch_tasks_page(query, cursor)
        out.extend(data.get("results", []))
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return out

# A filter query returns every task in every project you can see, including ones a
# collaborator has been assigned in a shared project. Rolling those would change someone
# else's due date from this account, so they are dropped before anything is considered:
# only tasks with no assignee, or assigned to us, are ours to move.
def is_ours(task):
    uid = task.get("responsible_uid")
    return uid is None or (MY_UID is not None and str(uid) == MY_UID)

overdue_tasks = fetch_tasks("overdue")
assigned_elsewhere = len(overdue_tasks)
overdue_tasks = [t for t in overdue_tasks if is_ours(t)]
assigned_elsewhere -= len(overdue_tasks)
if assigned_elsewhere:
    print(f"Leaving {assigned_elsewhere} overdue task(s) assigned to someone else alone.")

# Drop tasks older than the look-back window (the due date may carry a time component,
# e.g. "2026-01-07T19:00:00Z", so compare on the leading YYYY-MM-DD only).
def due_date_of(task):
    return ((task.get("due") or {}).get("date") or "")[:10]

# Tasks that have been pushed too many times are taken out before anything is rolled.
# Two kinds are never auto-backlogged however high the count:
#   * a prioritised task (p1/p2/p3) — the priority is an explicit "this one matters".
#   * a recurring task — coming back repeatedly is the whole point of one, and its
#     postponed_count climbs on every occurrence.
def is_exempt(task):
    return task.get("priority", 1) > 1 or (task.get("due") or {}).get("is_recurring", False)

def postponed(task):
    return task.get("postponed_count", 0) or 0

# Candidates deliberately reach wider than the roll set does:
#   * today's tasks as well as overdue ones, so a task on its 60th push is taken off the
#     list today rather than the day after it next goes overdue.
#   * tasks outside the ROLL_BACK_DAYS window too. Those are skipped by the roll for
#     manual triage, and a task 200 days overdue on its 90th push is the clearest case
#     there is of belonging on the Backlog page.
candidates = {t["id"]: t for t in overdue_tasks}
for t in fetch_tasks("date:today"):
    if is_ours(t):
        candidates.setdefault(t["id"], t)

stale = [t for t in candidates.values()
         if postponed(t) > ROLLOVER_LIMIT
         and not has_label(t, BACKLOG_LABEL)
         and not is_exempt(t)]
stale_ids = {t["id"] for t in stale}

if stale:
    print(f"Auto-backlogging {len(stale)} task(s) pushed more than {ROLLOVER_LIMIT} times: "
          f"tagging @{BACKLOG_LABEL} and clearing the due date.")

tasks = [t for t in overdue_tasks if t["id"] not in stale_ids]
total_overdue = len(tasks)
tasks = [t for t in tasks if due_date_of(t) >= cutoff_iso]
skipped = total_overdue - len(tasks)
if skipped:
    print(f"Skipping {skipped} task(s) overdue before {cutoff_iso} "
          f"(older than ROLL_BACK_DAYS={ROLL_BACK_DAYS}); left in place for manual triage.")

print(f"Found {len(tasks)} overdue task(s) to roll forward to {today_iso}.")

# Build Sync API commands — one per task.
# We preserve the full due object (string, is_recurring, lang, timezone) and only update
# the date. Using the Sync API item_update is required for recurring tasks; setting a
# due_date alone via the REST endpoint would strip the recurrence string.
def target_for(task):
    is_work_task = task.get("project_id") in work_project_ids
    is_weekend_tagged = has_label(task, WEEKEND_LABEL)
    if is_work_task and today_is_weekend:
        return upcoming_monday           # keep work off the weekend
    if is_weekend_tagged and not today_is_weekend:
        return upcoming_saturday         # keep @weekend tasks on the weekend
    return today_iso

# Two command lists, each paired with the tasks it came from, so the dry-run and the
# result report can name the right task for every command.
backlog_commands = []
for task in stale:
    labels = list(dict.fromkeys(task.get("labels", []) + [BACKLOG_LABEL]))
    backlog_commands.append({
        "type": "item_update",
        "uuid": str(uuid.uuid4()),
        "args": {"id": task["id"], "labels": labels, "due": None},
    })

# A due date may carry a time of day, either floating ("2026-09-24T11:00:00") or fixed to
# UTC ("2026-03-22T11:00:00Z"). Writing back a bare date silently drops that time, which
# takes any reminder hung off it with it, and leaves the due string still reading "at
# 19:00". So the time is carried across, and a fixed one is moved through the account
# timezone rather than by swapping the date in the UTC string: the stored instant means a
# local wall-clock time, and a roll across a DST boundary would otherwise shift it an hour.
def retarget(old_date, target_date):
    if "T" not in (old_date or ""):
        return target_date
    day = datetime.date.fromisoformat(target_date)
    clock = old_date.split("T", 1)[1]
    if clock.endswith("Z"):
        local = datetime.datetime.fromisoformat(old_date.replace("Z", "+00:00")).astimezone(TZ)
        moved = local.replace(year=day.year, month=day.month, day=day.day)
        return moved.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{target_date}T{clock}"

roll_commands = []
for task in tasks:
    due = task.get("due") or {}
    target_date = target_for(task)
    new_due = {
        "date": retarget(due.get("date"), target_date),
        "string": due.get("string", target_date),
        "is_recurring": due.get("is_recurring", False),
        "lang": due.get("lang", "en"),
    }
    if due.get("timezone"):
        new_due["timezone"] = due["timezone"]
    roll_commands.append({
        "type": "item_update",
        "uuid": str(uuid.uuid4()),
        "args": {"id": task["id"], "due": new_due},
    })

def finish(status=0):
    """Report naming problems last, where they are the final thing in the log, and fail
    the run so the wrapper logs FAILED and syslog gets an error rather than a quiet OK."""
    for problem in config_errors:
        print(f"CONFIG ERROR: {problem}", file=sys.stderr)
    sys.exit(1 if config_errors else status)

commands = backlog_commands + roll_commands
if not commands:
    print("Nothing to do.")
    finish()

if DRY_RUN:
    from collections import Counter
    if stale:
        print(f"[dry-run] would auto-backlog {len(stale)} task(s) "
              f"(@{BACKLOG_LABEL}, due date cleared):")
        for task in sorted(stale, key=lambda t: -postponed(t)):
            print(f"[dry-run]   pushed {postponed(task):4d}x  "
                  f"{due_date_of(task)}  {task['content'][:60]!r}")
    by_target = Counter(c["args"]["due"]["date"][:10] for c in roll_commands)
    print(f"[dry-run] would move {len(roll_commands)} task(s):")
    for tgt, n in sorted(by_target.items()):
        print(f"[dry-run]   -> {tgt}: {n} task(s)")
    for task, cmd in zip(tasks, roll_commands):
        old = (task.get("due") or {}).get("date")
        print(f"[dry-run]   {old} -> {cmd['args']['due']['date']}  {task['content'][:60]!r}")
    finish()

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
backlogged = []
errors = []
for task, cmd in list(zip(stale, backlog_commands)) + list(zip(tasks, roll_commands)):
    status = sync_status.get(cmd["uuid"], "unknown")
    target = backlogged if cmd["args"].get("due") is None else updated
    if status == "ok":
        target.append(task["content"])
    else:
        errors.append(f"{task['content']} (status: {status})")

if backlogged:
    print(f"Auto-backlogged {len(backlogged)} task(s) past {ROLLOVER_LIMIT} pushes: {backlogged}")
print(f"Rolled forward {len(updated)} task(s): {updated}")
if errors:
    print(f"Errors on {len(errors)} task(s): {errors}")
    finish(1)
finish()
