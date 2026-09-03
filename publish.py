#!/usr/bin/env python3
"""New PAC City — the publish step, and the only thing the Hearth ever runs.

    python3 publish.py

One command, forever. That is the whole design, and it is Amendment E of the
transport plan, which exists because two spike runs proved an agent-shaped
routine cannot be made prompt-free: each run surfaces a permission gate the
last one never reached, so approvals accumulate without ever converging. An
agent that composes its own commands has an open-ended command set, and an
open-ended command set cannot be pre-approved. So the routine's whole prompt
is "run publish.py and report what it printed" — the fetching, the story
list, the liveness record, the commit and the push all live in here, written
and reviewed in advance. Nothing improvises unattended.

What it does, in order:

  1. Makes the working tree match origin/main exactly, refusing to run if a
     human left uncommitted work here.
  2. Fetches every feed and writes story-list.json.
  3. Writes last-run.json — the liveness record (Amendment C).
  4. Commits and pushes both.

The push is the publish: a workflow in the repo renders the story list into
docs/ and commits the pages. This script never renders and never touches
docs/, so there is exactly one path onto the site and it runs through the
story list.

Why one command per git call and never a chained one: a compound command
matches no allowlist pattern, which is precisely how the first spike hung
(Amendment B).
"""
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import build

HERE = Path(__file__).parent
LIST_PATH = HERE / "story-list.json"
RUN_PATH = HERE / "last-run.json"
BRANCH = "main"

# Ours to overwrite on every run; anything else dirty is a human's work and
# stops the run rather than being discarded.
OURS = ("story-list.json", "last-run.json")


def git(*args, check=True):
    """One git command per call. Never chained — see the module docstring."""
    p = subprocess.run(("git", "-C", str(HERE)) + args,
                       capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError("git %s failed (%d): %s"
                           % (" ".join(args), p.returncode, p.stderr.strip()))
    return p.stdout.strip()


def content_hash(by_team):
    """Identity of the news itself, ignoring when it was fetched. Lets the run
    record say "ran and found nothing new" as distinct from "ran and
    published", which a story list cannot say on its own because its
    generated_at moves every time."""
    ids = sorted(i["id"] for items in by_team.values() for i in items)
    return hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()[:16]


def preflight():
    """Match origin/main exactly, or refuse. Deterministic input is the whole
    reason this can run unattended: a run that starts from a surprise state
    ends in one."""
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    if branch != BRANCH:
        raise RuntimeError("on branch %r, expected %r" % (branch, BRANCH))
    for name in OURS:
        git("checkout", "--", name, check=False)
    # Our own two artifacts never count as dirty, whether they are tracked,
    # modified or not there at all — a fresh clone has no story list yet, and
    # a run that refused to start over its own output would never start.
    dirty = [ln[3:] for ln in git("status", "--porcelain").splitlines()
             if ln[3:].strip('"') not in OURS]
    if dirty:
        raise RuntimeError("uncommitted work in the repo, refusing to reset: "
                           + ", ".join(dirty))
    git("fetch", "origin")
    git("reset", "--hard", "origin/" + BRANCH)


def write_run_record(rec):
    """The liveness signal, and it is deliberately a FILE rather than a claim.

    Amendment F: a run cannot perceive its own hang — from inside, a call that
    blocks for ninety minutes and one that returns in 200ms look identical,
    because the wait happens outside the conversation. So liveness is judged
    from wall clocks and artifacts on disk, never from the runner's account of
    itself. This file carries both wall clocks; its mtime is a third witness;
    the commit that pushes it is a fourth, readable from anywhere.

    Three outcomes are distinguishable, which is what Amendment C asked for:
    `published` and `no-new-stories` both say it ran, and a `finished_at`
    older than the last scheduled slot says it never did.

    This is not a run log. The append-only per-run telemetry line is stage 1
    of the sourcing plan and is deliberately absent here.
    """
    RUN_PATH.write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8")


def main():
    started = datetime.now(timezone.utc)
    rec = {"started_at": started.isoformat(), "finished_at": None,
           "outcome": "error", "detail": None, "feeds_ok": None,
           "feeds_total": None, "items": None, "content": None,
           "previous_content": None}
    try:
        preflight()
        cfg = json.loads((HERE / "feeds.json").read_text(encoding="utf-8"))

        previous = None
        if LIST_PATH.exists():
            try:
                prev_by_team, _ = build.read_story_list(LIST_PATH)
                previous = content_hash(prev_by_team)
            except Exception:
                pass                      # no readable previous list is not an error
        rec["previous_content"] = previous

        by_team, report = build.collect(cfg)
        for team, url, status, detail in report:
            print("[%s] %s: %s — %s" % (status, team, url, detail))
        ok = [r for r in report if r[2] == "ok"]
        total_items = sum(len(v) for v in by_team.values())
        rec.update(feeds_ok=len(ok), feeds_total=len(report), items=total_items)

        if not ok or total_items == 0:
            # Every feed failing at once is a network fault, not news. Publish
            # nothing rather than blank the site.
            raise RuntimeError("no feed produced any items; keeping the "
                               "previous story list")

        now = datetime.now(timezone.utc)
        build.write_story_list(by_team, now, LIST_PATH)
        current = content_hash(by_team)
        rec["content"] = current
        rec["outcome"] = "no-new-stories" if current == previous else "published"
        rec["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_run_record(rec)

        git("add", "story-list.json")
        git("add", "last-run.json")
        if git("diff", "--cached", "--name-only"):
            git("commit", "-m", "Fetch %s UTC — %d items from %d/%d feeds"
                % (now.strftime("%Y-%m-%d %H:%M"), total_items,
                   len(ok), len(report)))
            git("push", "origin", BRANCH)
            pushed = True
        else:
            pushed = False

        print("%s — %d items from %d/%d feeds; %s"
              % (rec["outcome"], total_items, len(ok), len(report),
                 "pushed" if pushed else "nothing to push"))
        return 0
    except Exception as exc:
        # The failure is recorded on disk before the exit, so a run that dies
        # leaves the same kind of evidence as one that succeeds.
        rec["detail"] = "%s: %s" % (type(exc).__name__, exc)
        rec["finished_at"] = datetime.now(timezone.utc).isoformat()
        try:
            write_run_record(rec)
        except Exception:
            pass
        print("ERROR: %s" % rec["detail"], file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
