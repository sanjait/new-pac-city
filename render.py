#!/usr/bin/env python3
"""New PAC City — render step.

Turns an existing story-list.json into the site's pages. Never fetches; run
fetch.py first (or `python3 build.py`, which still runs fetch and render
together in one command).

    python3 render.py
"""
import json
import sys
from pathlib import Path

import build

HERE = Path(__file__).parent


def main():
    cfg_path = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "feeds.json"
    cfg = json.loads(cfg_path.read_text())
    list_path = Path(sys.argv[2]) if len(sys.argv) > 2 else HERE / "story-list.json"
    build.render(cfg, list_path)


if __name__ == "__main__":
    main()
