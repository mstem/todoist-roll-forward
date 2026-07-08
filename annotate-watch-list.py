#!/usr/bin/env python3
"""
Annotates tasks in the Todoist #watch project with media metadata:
type (Movie/TV), year, genre, runtime, and streaming availability (PT then US).

Usage:
  python3 annotate-watch-list.py            # annotate all tasks
  python3 annotate-watch-list.py --limit 8  # test batch
"""

import json
import os
import re
import sys
import time
import uuid
import subprocess
import argparse

import anthropic
import urllib.request
import urllib.parse

# Load .env
env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip()

TODOIST_TOKEN = os.environ.get("TODOIST_API_TOKEN")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")
TMDB_KEY = os.environ.get("TMDB_API_KEY")
WATCH_PROJECT_ID = "6QWqxq5w9jMVRvQC"

for name, val in [("TODOIST_API_TOKEN", TODOIST_TOKEN), ("ANTHROPIC_API_KEY", ANTHROPIC_KEY), ("TMDB_API_KEY", TMDB_KEY)]:
    if not val:
        print(f"ERROR: {name} not set", file=sys.stderr)
        sys.exit(1)

ai_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)


# ── TMDB helpers ────────────────────────────────────────────────────────────

def tmdb_get(path, params=None):
    base = "https://api.themoviedb.org/3"
    p = {"api_key": TMDB_KEY}
    if params:
        p.update(params)
    url = f"{base}{path}?{urllib.parse.urlencode(p)}"
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            return json.loads(resp.read())
    except Exception:
        return {}


def search_tmdb(title):
    """Return (media_type, tmdb_id) for the best match, or (None, None)."""
    data = tmdb_get("/search/multi", {"query": title, "language": "en-US"})
    results = [r for r in data.get("results", []) if r.get("media_type") in ("movie", "tv")]
    if not results:
        return None, None
    # Prefer exact-ish title match; fall back to first result (sorted by popularity)
    results.sort(key=lambda r: r.get("popularity", 0), reverse=True)
    best = results[0]
    return best["media_type"], best["id"]


def get_movie_details(tmdb_id):
    return tmdb_get(f"/movie/{tmdb_id}", {"language": "en-US"})


def get_tv_details(tmdb_id):
    return tmdb_get(f"/tv/{tmdb_id}", {"language": "en-US"})


def get_watch_providers(media_type, tmdb_id):
    """Return list of streaming service names, PT first then US fallback."""
    data = tmdb_get(f"/{media_type}/{tmdb_id}/watch/providers")
    results = data.get("results", {})
    names = []
    seen = set()
    for country in ("PT", "US"):
        flatrate = results.get(country, {}).get("flatrate", [])
        for p in flatrate:
            name = p.get("provider_name", "")
            if name and name not in seen:
                names.append(name)
                seen.add(name)
    return names


def fmt_runtime(minutes):
    if not minutes:
        return None
    h, m = divmod(int(minutes), 60)
    return f"{h}h {m}m" if h else f"{m}m"


def fmt_year(date_str):
    if date_str and len(date_str) >= 4:
        return date_str[:4]
    return "?"


def top_genres(genres, n=2):
    return ", ".join(g["name"] for g in (genres or [])[:n])


# ── Anthropic title extraction ───────────────────────────────────────────────

def extract_titles_batch(items):
    """
    items: list of (task_id, raw_content)
    Returns dict {task_id: search_query_or_SKIP}
    """
    numbered = "\n".join(f"{i+1}. {content}" for i, (_, content) in enumerate(items))
    prompt = f"""Each line is a task from a personal watch list. Extract the title to search for.

Rules:
- Output one line per item: just the clean title, nothing else
- Strip personal notes like "(for renata)", "(adult)", "(Boston)", "- 1998", streaming hints like "(netflix)"
- If multiple titles are listed (e.g. "sicario and strangelove"), output only the first one
- If it's a YouTube video, CCC talk, Loom link, Reddit link, or clearly not a film/show, output: SKIP
- If it's a vague list like "Almodovar's Madrid movies:" with no specific title, output: SKIP
- If it's a director/actor name with no specific title, output: SKIP
- Do not add quotes or explanation. One line per item only.

Items:
{numbered}"""

    resp = ai_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}]
    )
    lines = resp.content[0].text.strip().splitlines()
    result = {}
    for i, (task_id, _) in enumerate(items):
        raw = lines[i].strip() if i < len(lines) else "SKIP"
        raw = re.sub(r'^\d+\.\s*', '', raw)
        result[task_id] = None if raw.upper() == "SKIP" else raw
    return result


# ── Todoist helpers ──────────────────────────────────────────────────────────

def fetch_all_tasks():
    tasks = []
    cursor = None
    while True:
        url = f"https://api.todoist.com/api/v1/tasks?project_id={WATCH_PROJECT_ID}&limit=200"
        if cursor:
            url += f"&cursor={cursor}"
        r = subprocess.run(["curl", "-s", url, "-H", f"Authorization: Bearer {TODOIST_TOKEN}"],
                           capture_output=True, text=True)
        data = json.loads(r.stdout)
        tasks.extend(data.get("results", []))
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return tasks


def sync_update(commands):
    r = subprocess.run(
        ["curl", "-s", "-X", "POST", "https://api.todoist.com/api/v1/sync",
         "-H", f"Authorization: Bearer {TODOIST_TOKEN}",
         "-H", "Content-Type: application/json",
         "-d", json.dumps({"commands": commands})],
        capture_output=True, text=True
    )
    return json.loads(r.stdout)


# ── Main ─────────────────────────────────────────────────────────────────────

def build_description(media_type, details, providers):
    if not details:
        return None

    if media_type == "movie":
        year = fmt_year(details.get("release_date", ""))
        genres = top_genres(details.get("genres"))
        runtime = fmt_runtime(details.get("runtime"))
        parts = ["Movie", year, genres]
        if runtime:
            parts.append(runtime)
    else:
        year = fmt_year(details.get("first_air_date", ""))
        genres = top_genres(details.get("genres"))
        seasons = details.get("number_of_seasons", "")
        eps = details.get("number_of_episodes", "")
        length = f"S{seasons}" if seasons else None
        if eps and seasons and seasons > 1:
            length = f"{seasons} seasons, {eps} eps"
        elif eps:
            length = f"{eps} eps"
        parts = ["TV", year, genres]
        if length:
            parts.append(length)

    if providers:
        parts.append(", ".join(providers[:3]))

    return " · ".join(p for p in parts if p)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    print("Fetching tasks from #watch project...")
    all_tasks = fetch_all_tasks()
    print(f"Found {len(all_tasks)} tasks.")

    to_process = [(t["id"], t["content"]) for t in all_tasks]
    if args.limit:
        to_process = to_process[:args.limit]

    # Step 1: extract clean search titles via Haiku
    print("Extracting titles...")
    BATCH = 20
    title_map = {}
    for i in range(0, len(to_process), BATCH):
        batch = to_process[i:i + BATCH]
        title_map.update(extract_titles_batch(batch))

    skipped = [tid for tid, t in title_map.items() if t is None]
    to_lookup = [(tid, title) for tid, title in title_map.items() if title is not None]
    print(f"  {len(to_lookup)} to look up on TMDB, {len(skipped)} skipped.")

    # Step 2: look up each on TMDB
    print("Looking up on TMDB...")
    task_content = {t["id"]: t["content"] for t in all_tasks}
    commands = []
    not_found = []

    for task_id, search_title in to_lookup:
        media_type, tmdb_id = search_tmdb(search_title)
        if not tmdb_id:
            not_found.append(task_content.get(task_id, task_id))
            continue

        if media_type == "movie":
            details = get_movie_details(tmdb_id)
        else:
            details = get_tv_details(tmdb_id)

        providers = get_watch_providers(media_type, tmdb_id)
        desc = build_description(media_type, details, providers)

        if desc:
            commands.append({
                "type": "item_update",
                "uuid": str(uuid.uuid4()),
                "args": {"id": task_id, "description": desc}
            })

        time.sleep(0.1)  # gentle rate limiting

    # Preview
    print(f"\nAnnotating {len(commands)} tasks ({len(not_found)} not found on TMDB).")
    print("\nSample annotations:")
    for cmd in commands[:10]:
        tid = cmd["args"]["id"]
        print(f"  [{task_content.get(tid,'?')[:50]}]")
        print(f"    → {cmd['args']['description']}")

    if not_found:
        print(f"\nNot found on TMDB: {not_found}")

    if not commands:
        print("Nothing to update.")
        return

    # Step 3: batch update Todoist
    SYNC_BATCH = 100
    total_ok = total_err = 0
    for i in range(0, len(commands), SYNC_BATCH):
        batch = commands[i:i + SYNC_BATCH]
        data = sync_update(batch)
        statuses = data.get("sync_status", {})
        total_ok += sum(1 for s in statuses.values() if s == "ok")
        total_err += sum(1 for s in statuses.values() if s != "ok")

    print(f"\nDone. {total_ok} updated, {total_err} errors.")


if __name__ == "__main__":
    main()
