#!/usr/bin/env python3
"""
Annotates tasks in the Todoist #read project with book metadata:
Fiction/Non-fiction, year published, and genre.

Usage:
  python3 annotate-read-list.py            # annotate all tasks
  python3 annotate-read-list.py --limit 8  # test batch of 8 tasks
"""

import json
import os
import sys
import uuid
import subprocess
import argparse
import re

# Load .env manually so we can run without --env-file
env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip()

import anthropic

TODOIST_TOKEN = os.environ.get("TODOIST_API_TOKEN")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")
READ_PROJECT_ID = "6QfMpc9xW653MV9w"
BATCH_CLASSIFY = 20
BATCH_SYNC = 100

if not TODOIST_TOKEN:
    print("ERROR: TODOIST_API_TOKEN not set", file=sys.stderr)
    sys.exit(1)
if not ANTHROPIC_KEY:
    print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
    sys.exit(1)


def curl_get(url):
    r = subprocess.run(
        ["curl", "-s", url, "-H", f"Authorization: Bearer {TODOIST_TOKEN}"],
        capture_output=True, text=True
    )
    return json.loads(r.stdout)


def fetch_all_tasks():
    tasks = []
    cursor = None
    while True:
        url = f"https://api.todoist.com/api/v1/tasks?project_id={READ_PROJECT_ID}&limit=200"
        if cursor:
            url += f"&cursor={cursor}"
        data = curl_get(url)
        tasks.extend(data.get("results", []))
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return tasks


def is_skippable(content):
    """Return True for non-book entries that should not be annotated."""
    c = content.strip()
    # Pure URL (markdown link or bare http)
    if re.match(r'^\[?https?://', c):
        return True
    # Action reminders
    action_prefixes = ("download ", "load ", "search ", "start reading ", "get ")
    if c.lower().startswith(action_prefixes):
        return True
    return False


def classify_batch(items):
    """
    items: list of (task_id, content)
    Returns dict {task_id: "Fiction · 1985 · Dystopian novel"} or {task_id: None} for SKIP
    """
    numbered = "\n".join(f"{i+1}. {content}" for i, (_, content) in enumerate(items))
    prompt = f"""You are classifying items from a personal reading list. For each numbered item, output exactly one line in this format:

  Fiction|YEAR|GENRE
  Non-fiction|YEAR|GENRE
  SKIP

Rules:
- Use Fiction or Non-fiction based on the title
- YEAR: 4-digit best guess at original publication year; use ? only if truly unknown
- GENRE: 1–3 words (e.g. Self-help, Literary fiction, History, Fantasy, Philosophy, Science)
- SKIP if: the item is a URL, a task/reminder rather than a book title, an author name without a specific title, or a book list rather than a single title
- Output ONLY the format above, one line per item, numbered to match. No extra text.

Items:
{numbered}"""

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}]
    )
    lines = response.content[0].text.strip().splitlines()

    result = {}
    for i, (task_id, _) in enumerate(items):
        raw = lines[i].strip() if i < len(lines) else "SKIP"
        # Strip leading "N. " if model included it
        raw = re.sub(r'^\d+\.\s*', '', raw)
        if raw.upper() == "SKIP" or not raw:
            result[task_id] = None
        else:
            parts = raw.split("|")
            if len(parts) == 3:
                ftype, year, genre = [p.strip() for p in parts]
                result[task_id] = f"{ftype} · {year} · {genre}"
            else:
                result[task_id] = None
    return result


def sync_update(commands):
    r = subprocess.run(
        ["curl", "-s", "-X", "POST",
         "https://api.todoist.com/api/v1/sync",
         "-H", f"Authorization: Bearer {TODOIST_TOKEN}",
         "-H", "Content-Type: application/json",
         "-d", json.dumps({"commands": commands})],
        capture_output=True, text=True
    )
    return json.loads(r.stdout)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="Only process first N tasks")
    args = parser.parse_args()

    print("Fetching tasks from #read project...")
    all_tasks = fetch_all_tasks()
    print(f"Found {len(all_tasks)} tasks total.")

    # Filter out clearly non-book entries
    to_process = [(t["id"], t["content"]) for t in all_tasks if not is_skippable(t["content"])]
    if args.limit:
        to_process = to_process[:args.limit]
    print(f"Processing {len(to_process)} tasks (after filtering {len(all_tasks) - len(to_process) - (len(all_tasks) - len([t for t in all_tasks if not is_skippable(t['content'])]))} skippable).")

    # Classify in batches
    annotations = {}
    for i in range(0, len(to_process), BATCH_CLASSIFY):
        batch = to_process[i:i + BATCH_CLASSIFY]
        print(f"  Classifying batch {i//BATCH_CLASSIFY + 1} ({len(batch)} titles)...")
        result = classify_batch(batch)
        annotations.update(result)

    # Build sync commands
    commands = []
    skipped = []
    for task_id, desc in annotations.items():
        if desc is None:
            skipped.append(task_id)
            continue
        commands.append({
            "type": "item_update",
            "uuid": str(uuid.uuid4()),
            "args": {"id": task_id, "description": desc}
        })

    print(f"\nAnnotating {len(commands)} tasks, skipping {len(skipped)} (not identifiable as books).")

    # Preview
    print("\nSample annotations:")
    task_map = {t["id"]: t["content"] for t in all_tasks}
    for cmd in commands[:10]:
        tid = cmd["args"]["id"]
        print(f"  [{task_map.get(tid, tid)[:50]}]  →  {cmd['args']['description']}")

    if not commands:
        print("Nothing to update.")
        return

    # Send in batches
    total_ok = 0
    total_err = 0
    for i in range(0, len(commands), BATCH_SYNC):
        batch = commands[i:i + BATCH_SYNC]
        data = sync_update(batch)
        statuses = data.get("sync_status", {})
        ok = sum(1 for s in statuses.values() if s == "ok")
        err = sum(1 for s in statuses.values() if s != "ok")
        total_ok += ok
        total_err += err
        if err:
            for cmd in batch:
                uid = cmd["uuid"]
                if statuses.get(uid) != "ok":
                    print(f"  ERROR on [{task_map.get(cmd['args']['id'], '?')}]: {statuses.get(uid)}")

    print(f"\nDone. {total_ok} updated, {total_err} errors.")


if __name__ == "__main__":
    main()
