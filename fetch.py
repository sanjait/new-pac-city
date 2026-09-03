#!/usr/bin/env python3
"""New PAC City — fetch step.

Reads every feed in feeds.json over the network and writes story-list.json:
what render.py (or `python3 build.py`, which still runs fetch and render
together in one command) turns into pages without touching the network again.

    python3 fetch.py
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import build

HERE = Path(__file__).parent


def main():
    cfg_path = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "feeds.json"
    cfg = json.loads(cfg_path.read_text())
    list_path = HERE / "story-list.json"
    now = datetime.now(timezone.utc)
    by_team, report = build.collect(cfg)
    ok = [r for r in report if r[2] == "ok"]
    for team, url, status, detail in report:
        print(f"[{status}] {team}: {url} — {detail}")
    total_items = sum(len(v) for v in by_team.values())
    if not ok or total_items == 0:
        print("ERROR: no feed produced any items; keeping the previous story list.", file=sys.stderr)
        sys.exit(1)
    build.write_story_list(by_team, now, list_path)
    print(f"Wrote story-list.json — {total_items} items from {len(ok)}/{len(report)} feeds.")


if __name__ == "__main__":
    main()
