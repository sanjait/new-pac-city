#!/usr/bin/env python3
"""New PAC City — the item-reviewer runner, and the whole of what runs
unattended (plan-item-review.md §8.7).

    python3 review_cycle.py prepare
    python3 review_cycle.py finish

Two fixed commands, same argv every run, and all git work lives inside them.
Between the two, a judge — a human or an agent session, never a shell —
reads a reviewer prompt and a set of plain-text batch views and writes one
JSON output file per batch. Nothing in this file judges anything: `prepare`
only syncs, selects, fetches and writes; `finish` only validates, merges and
publishes what the judge wrote.

Why two fixed commands rather than one script that also judges: two spike
runs (2026-08-24) showed a routine that composes its own commands stalls on
a permission gate the last run never reached, and gates apply per tool call,
not to what a trusted script does internally — `publish.py` already pushes
to this same public repo, unattended, twice a day. So the git work that
would otherwise need a prompt-free `git push` lives inside this script,
exactly as `publish.py`'s does (see its `git()` helper and `preflight()`,
copied in shape below), and the judging step is file reads and writes only —
nothing to gate.

`prepare`:
  1. Fetches origin and fast-forwards this branch to it. Never resets, never
     forces; a dirty tree or a diverged history is reported and refused.
  2. Calls review.py's own `prep` (selection, fetch, batching) unchanged.
  3. Converts each batch-NN.json into a plain-text, line-wrapped judge view
     (view-NN.txt), body capped at 15,000 characters with the cut marked —
     the batch JSON puts one item on a single line up to ~97k characters,
     which a line-oriented reader tool truncates.
  4. Prints exactly which file to read (the reviewer prompt, and only the
     prompt — not the schema, not the registry table; the prompt already
     carries both) and which view file maps to which required output file.

`finish`:
  1. Same sync as `prepare`.
  2. Refuses outright — before writing anything — if the working tree
     carries a change to any file this script does not own (verdicts.json).
  3. Validates every review-work/out-NN.json with review.py's own merge
     logic, appends the new rows to the existing verdicts.json (a row that
     already exists is never rewritten), writes the extended run record, the
     hidden-if-live report and the sweep record.
  4. Commits verdicts.json only and pushes — following publish.py's pattern
     exactly: one git call per command, never chained.
  5. Is a no-op the second time: if every judged id already carries a
     verdict, nothing is written, nothing is committed, nothing is pushed.

Stdlib only, matching the rest of this repo.
"""
import argparse
import json
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import build
import review

# The reviewer prompt lives in the Studio's own document tree, not in this
# repo — it is a Studio project document (projects/new-pac-city/), read by
# whoever/whatever is doing the judging between `prepare` and `finish`, never
# by this script. This is the stable, non-lane path: the Studio checkout at
# C:\studio\the-studio, sibling to this repo under C:\studio. A lane's copy
# under .claude/worktrees/<lane>/... is not durable and must never be printed
# here — see CLAUDE.md's "a link you give the CEO must open."
REVIEWER_PROMPT_PATH = (
    Path(review.__file__).resolve().parent.parent
    / "the-studio" / "projects" / "new-pac-city" / "work-reviewer-prompt.md"
)

# The only file `finish` may commit. Everything else `finish` writes
# (review-work/*) is in .gitignore and never reaches git status at all.
OWNED_FILES = ("verdicts.json",)

VIEW_WRAP_WIDTH = 100
BODY_CHAR_CAP = 15000


# --------------------------------------------------------------------- git
#
# Copied in shape from publish.py's own git()/preflight(): one git command
# per call (a compound command matches no allowlist pattern and is how the
# first spike hung — publish.py's Amendment B), and a repo parameter so
# tests can point this at a throwaway git repo rather than the real one.

def git(repo, *args, check=True, raw=False):
    p = subprocess.run(("git", "-C", str(repo)) + args,
                        capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError("git %s failed (%d): %s"
                            % (" ".join(args), p.returncode, p.stderr.strip()))
    return p.stdout if raw else p.stdout.strip()


def _porcelain_paths(repo):
    """Every dirty path from `git status --porcelain`, unquoted. Uses the
    raw (unstripped) form because the leading status columns are fixed-width
    and a stripped line shifts the filename by one character — the same
    reason publish.py's own git() carries a `raw` mode."""
    return [ln[3:].strip('"') for ln in git(repo, "status", "--porcelain", raw=True).splitlines()
            if ln.strip()]


SITE_BRANCH = "main"


def sync_with_origin(repo):
    """Bring `repo` level with the branch the SITE publishes from — `main` —
    whatever the local branch is called. Fetches, then fast-forwards ONLY:
    never `reset --hard`, never `push --force`. A dirty tree, or a history
    that has genuinely diverged, is refused rather than resolved by
    discarding anything, and the caller is told what stood in the way.
    Returns the local branch name.

    **Why `main` and not the local branch's own remote** (found in the
    attended run, 2026-09-11): the reviewer's input is the story list the
    site publishes, which only ever lives on `main`. A local staging branch
    may have no remote at all — `review-activation` never did, because its
    work reaches the site as `push HEAD:main` — and syncing against
    `origin/<local branch>` failed on the first real invocation."""
    dirty = _porcelain_paths(repo)
    if dirty:
        raise RuntimeError("working tree is not clean, refusing to sync: %s" % ", ".join(dirty))

    git(repo, "fetch", "origin")
    branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    remote_ref = "origin/%s" % SITE_BRANCH
    try:
        git(repo, "rev-parse", "--verify", remote_ref)
    except RuntimeError:
        raise RuntimeError("no %s to sync against — the site's branch is missing" % remote_ref)

    try:
        git(repo, "merge", "--ff-only", remote_ref)
    except RuntimeError as exc:
        ahead = git(repo, "log", "--oneline", "%s..HEAD" % remote_ref, check=False)
        detail = ("; this branch carries commit(s) not on %s: %s" % (remote_ref, ahead)) if ahead else ""
        raise RuntimeError(
            "cannot fast-forward to %s without discarding local history — "
            "refusing to reset or force%s" % (remote_ref, detail))
    return branch


# -------------------------------------------------------------- judge views

def _wrap(text):
    if not text:
        return ""
    paragraphs = text.split("\n\n")
    wrapped = [textwrap.fill(p, width=VIEW_WRAP_WIDTH, break_long_words=True,
                              break_on_hyphens=False)
               for p in paragraphs]
    return "\n\n".join(wrapped)


def write_judge_view(batch_path, view_path):
    """Convert one batch-NN.json into a plain-text view a judge — human or
    agent — can read with an ordinary line-oriented tool: every line short,
    the body capped at BODY_CHAR_CAP characters with the cut stated rather
    than silently truncated by a reader tool that never gets told.

    This folds in, as a proper part of the runner, the ad hoc converter
    written in a scratchpad on 2026-09-11 (plan-item-review.md §8.7) — same
    shape of output, now generated on every prepare rather than by hand."""
    data = json.loads(batch_path.read_text(encoding="utf-8"))
    items = data.get("items", [])

    lines = ["# %s — %d item%s. Judge every one. Item ids are exact; copy them verbatim."
              % (batch_path.stem.replace("batch-", "Batch "), len(items),
                 "" if len(items) == 1 else "s"),
              ""]

    for i, entry in enumerate(items, 1):
        shown = entry.get("shown_to_reviewer", {})
        lines.append("=" * 78)
        lines.append("ITEM %d of %d   id: %s" % (i, len(items), entry.get("id")))
        for label, key in (("title", "title"), ("source", "source"), ("feed_team", "feed_team"),
                            ("feed_sport", "feed_sport"), ("medium", "medium"), ("class", "class"),
                            ("date", "date"), ("link", "link"), ("evidence_basis", "evidence_basis")):
            lines.append("%s: %s" % (label, shown.get(key) if shown.get(key) is not None else ""))
        lines.append("summary:")
        lines.append(_wrap(shown.get("summary")) or "(none)")

        body = shown.get("body")
        if body:
            cut = len(body) > BODY_CHAR_CAP
            shown_body = body[:BODY_CHAR_CAP]
            lines.append("body (%d chars%s):" % (len(body), ", CUT at %d" % BODY_CHAR_CAP if cut else ""))
            lines.append(_wrap(shown_body))
            if cut:
                lines.append("[... CUT — %d of %d characters omitted ...]" % (len(body) - BODY_CHAR_CAP, len(body)))
        else:
            lines.append("body: (none)")
        lines.append("")

    view_path.write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------- prepare

def prepare_body():
    """Everything `prepare` does except the git sync: run review.py's own
    `prep`, then convert each batch it wrote into a judge view and print the
    judging instructions. Split out so it can be tested directly against a
    temp review.HERE/review.REVIEW_WORK without needing a git repo at all."""
    prep_args = argparse.Namespace(story_list=str(review.HERE / "story-list.json"))
    review.cmd_prep(prep_args)

    stats_path = review.REVIEW_WORK / "prep-stats.json"
    stats = json.loads(stats_path.read_text(encoding="utf-8")) if stats_path.exists() else {}
    batches_written = stats.get("batches_written", 0)

    print()
    if not batches_written:
        print("Nothing to do: every independent-text item already carries a verdict. "
              "No batch written.")
        return

    batch_paths = sorted(review.REVIEW_WORK.glob("batch-*.json"))
    for old_view in review.REVIEW_WORK.glob("view-*.txt"):
        old_view.unlink()

    view_paths = []
    for batch_path in batch_paths:
        view_path = review.REVIEW_WORK / batch_path.name.replace("batch-", "view-").replace(".json", ".txt")
        write_judge_view(batch_path, view_path)
        view_paths.append(view_path)

    print("=" * 78)
    print("JUDGING STEP")
    print("=" * 78)
    print()
    print("1. Read this file, and ONLY this file — not the schema, not the program registry;")
    print("   the prompt already carries both:")
    print("     %s" % REVIEWER_PROMPT_PATH)
    print()
    print("2. Judge every item in each view file below, and write one output file per batch,")
    print("   at the exact path shown, shaped exactly like this (one entry per item id in that")
    print("   batch; omit `ambiguous` unless the call was genuinely close):")
    print()
    print('   {"verdicts": {"<item id>": {')
    print('       "subjects": {"<registry key>": "about" | "involves", ...},')
    print('       "unrecognized": [{"phrase": "...", "kind": "unlisted-program" | "not-ours"}, ...],')
    print('       "kind": "column" | "preview" | "recap" | "news",')
    print('       "verdict": "publish" | "hold" | "drop",')
    print('       "reason": "one sentence",')
    print('       "evidence_basis": "title-only" | "title+summary" | "title+full-text",')
    print('       "ambiguous": "one sentence (omit if not close)"')
    print('   }}}')
    print()
    print("   Batch -> view to judge -> output to write:")
    for batch_path, view_path in zip(batch_paths, view_paths):
        out_path = review.REVIEW_WORK / batch_path.name.replace("batch-", "out-")
        print("     %s  ->  %s" % (view_path, out_path))
    print()
    print("3. Once every out-NN.json above exists, run:  python3 review_cycle.py finish")


def archive_prior_run(work_dir):
    """Move a previous run's batch/view/out files aside before writing new
    ones, and say where they went.

    **Found in the attended run, 2026-09-11, and it is the sharpest defect
    that run turned up.** `prepare` wrote fresh batch-01/02 and view-01/02
    beside TEN stale out-NN.json files from an earlier pass. Two things then
    went wrong at once: the judge assigned batch 02 found its output path
    already holding 24 verdicts for somebody else's items (it refused to
    overwrite them, correctly, and stopped), and `finish` would have fed the
    eight leftover out-files to the merge — harmless this once, because those
    rows were already in verdicts.json and the merge appends, but a stale
    verdict re-merged is exactly the drift this design exists to prevent.

    Archive rather than delete: the files are a run's evidence."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    stale = sorted(p for pattern in ("batch-*.json", "view-*.txt", "out-*.json")
                   for p in work_dir.glob(pattern))
    if not stale:
        return None
    dest = work_dir / "prior" / stamp
    dest.mkdir(parents=True, exist_ok=True)
    for p in stale:
        p.rename(dest / p.name)
    print("Archived %d file(s) from a previous run to %s" % (len(stale), dest))
    print()
    return dest


def cmd_prepare(args):
    branch = sync_with_origin(review.HERE)
    print("Branch %r level with origin/%s." % (branch, SITE_BRANCH))
    print()
    archive_prior_run(review.REVIEW_WORK)
    prepare_body()


# ---------------------------------------------------------------------- finish

def load_existing_verdicts_raw(path):
    """(verdicts, runs) currently on disk, or ({}, {}) if the file does not
    exist. Unlike build.read_verdicts (which silently treats a malformed
    file as empty — correct for rendering, where a bad file must never take
    the site down), `finish` raises here: silently treating an existing,
    but unreadable, verdicts.json as empty would let this script overwrite
    232 real rows with a run that only ever saw a handful of new ones. An
    absent file is not the same failure and is not an error."""
    if not path.exists():
        return {}, {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError("%s exists but is not readable JSON (%s) — refusing to merge "
                            "over it rather than risk losing existing rows" % (path.name, exc))
    verdicts = data.get("verdicts", {})
    runs = data.get("runs", {})
    if not isinstance(verdicts, dict):
        raise RuntimeError("%s's \"verdicts\" is not an object — refusing to merge over it"
                            % path.name)
    if not isinstance(runs, dict):
        runs = {}
    return verdicts, runs


def check_no_foreign_dirt(repo):
    """Refuse outright if the working tree carries a change to any file this
    script does not own. Raises rather than exits, so it can be tested and
    so `finish` refuses before it writes anything, not partway through."""
    offending = [p for p in _porcelain_paths(repo) if p not in OWNED_FILES]
    if offending:
        raise RuntimeError("the working tree carries a change to a file this script does not "
                            "own: %s" % ", ".join(offending))


def merge_and_write(args):
    """The non-git heart of `finish`: validate the judge's output, merge it
    onto the existing verdicts.json (adding only — an existing row is never
    rewritten), write the run record, the hidden-if-live report and the
    sweep record. Returns a dict describing what changed, for the caller to
    build a commit message from, or None if there was nothing to do (no
    out-*.json files, or every judged id already carries a verdict) — the
    two shapes of no-op `finish` must handle without touching git at all.

    Split out from cmd_finish so it can be tested directly against a temp
    review.HERE/review.REVIEW_WORK without ever calling git — this repo's
    tests must never push, and this function never even tries to."""
    out_paths = sorted(review.REVIEW_WORK.glob("out-*.json"))
    if not out_paths:
        print("No judge output found (review-work/out-*.json). Nothing to finish.")
        return None

    batches_by_id = review.load_batches()
    registry_keys = review.load_registry_keys()
    good, bad, warnings = review.merge_outputs(out_paths, batches_by_id, registry_keys)

    verdicts_path = review.HERE / build.VERDICTS_FILE
    existing_verdicts, existing_runs = load_existing_verdicts_raw(verdicts_path)

    new_ids = sorted(i for i in good if i not in existing_verdicts)
    already_had = len(good) - len(new_ids)

    if not new_ids:
        print("Nothing new: every judged id already carries a verdict in %s. No-op — "
              "nothing written, nothing committed, nothing pushed." % verdicts_path.name)
        return None

    new_good = {item_id: good[item_id] for item_id in new_ids}

    run_id = "run_%s" % datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M")
    for item_id in new_good:
        new_good[item_id]["run"] = run_id

    stats = {}
    stats_path = review.REVIEW_WORK / "prep-stats.json"
    if stats_path.exists():
        stats = json.loads(stats_path.read_text(encoding="utf-8"))

    verdict_counts, hidden_per_source, unrecognized_queue = {}, {}, []
    for item_id, row in new_good.items():
        v = row.get("verdict")
        verdict_counts[v] = verdict_counts.get(v, 0) + 1
        entry = batches_by_id.get(item_id)
        source = (entry["shown_to_reviewer"]["source"] if entry else None) or "(unknown source)"
        if v in ("drop", "hold"):
            hidden_per_source[source] = hidden_per_source.get(source, 0) + 1
        for u in (row.get("unrecognized") or []):
            unrecognized_queue.append({"id": item_id, "source": source,
                                        "phrase": u.get("phrase"), "kind": u.get("kind")})

    rows_with_warnings = len({w["id"] for w in warnings})

    # The script cannot measure tokens itself (plan-schema-v3 §6.2's
    # four-hour lesson: an agent's self-reported billing total is not the
    # cost of the work). It records only what the caller supplies, and
    # where that number came from — never a figure of its own invention.
    run_record = {
        "schema": review.SCHEMA_VERSION,
        "prompt": review.PROMPT_VERSION,
        "model": review.MODEL,
        "started": stats.get("generated_at") or datetime.now(timezone.utc).isoformat(),
        "items": len(new_good) + len(bad),
        "verdict_counts": verdict_counts,
        "hidden_per_source": hidden_per_source,
        "unreadable_per_source": stats.get("per_source_unreadable", {}),
        "unrecognized": unrecognized_queue,
        "bad_rows_excluded": len(bad),
        "rows_already_verdicted": already_had,
        "rows_with_warnings": rows_with_warnings,
        "warnings": warnings,
        "wall_clock_prep_s": stats.get("wall_clock_prep_s"),
        "tokens": args.tokens,
        "tokens_source": args.tokens_source,
    }

    merged_verdicts = dict(existing_verdicts)
    merged_verdicts.update(new_good)  # adds only; every existing id is untouched above
    merged_runs = dict(existing_runs)
    merged_runs[run_id] = run_record

    verdicts_path.write_text(
        json.dumps({"verdicts": merged_verdicts, "runs": merged_runs}, indent=2, ensure_ascii=False),
        encoding="utf-8")

    review.REVIEW_WORK.mkdir(exist_ok=True)
    (review.REVIEW_WORK / "hidden-if-live.md").write_text(
        review.build_hidden_if_live(new_good, [b[0] for b in bad], batches_by_id, run_id),
        encoding="utf-8")
    sweep_md, sweep_data = review.build_sweep(new_good, batches_by_id, run_id)
    (review.REVIEW_WORK / ("sweep-%s.md" % run_id)).write_text(sweep_md, encoding="utf-8")
    (review.REVIEW_WORK / ("sweep-%s.json" % run_id)).write_text(
        json.dumps(sweep_data, indent=2, ensure_ascii=False), encoding="utf-8")

    print("Merged %d new row(s) (%d already verdicted, %d excluded, %d carried warnings)."
          % (len(new_good), already_had, len(bad), rows_with_warnings))
    if bad:
        print()
        print("Excluded rows:")
        for item_id, problems in bad:
            print("  %s: %s" % (item_id, "; ".join(problems)))
    if warnings:
        print()
        print("Warnings (row kept):")
        for w in warnings:
            print("  %s [%s]: %s" % (w["id"], w["source"], w["problem"]))
    print()
    print("Run record %s:" % run_id)
    print(json.dumps(run_record, indent=2, ensure_ascii=False))

    return {"run_id": run_id, "new_good": new_good, "verdict_counts": verdict_counts}


def _push_if_ahead(repo, branch):
    """Push only if HEAD actually carries something origin/<branch> does
    not — covers both "this run just committed" and "a PRIOR run committed
    but its own push failed (a network blip, say), and this run found
    nothing new to merge." Without this second case, a failed push would
    never be retried: `finish` would see no new rows, commit nothing, and
    quietly leave an already-committed run stuck local-only forever."""
    ahead = git(repo, "rev-list", "--count", "origin/%s..HEAD" % SITE_BRANCH)
    if ahead != "0":
        # HEAD:main, not branch:branch — the site publishes from `main` and a
        # local staging branch may have no remote of its own. Same reason
        # sync_with_origin() reads origin/main. (Attended run, 2026-09-11.)
        git(repo, "push", "origin", "HEAD:%s" % SITE_BRANCH)
        print()
        print("Pushed to origin/%s." % SITE_BRANCH)
    else:
        print()
        print("Nothing to push.")


def cmd_finish(args):
    branch = sync_with_origin(review.HERE)
    check_no_foreign_dirt(review.HERE)

    result = merge_and_write(args)
    if result is not None:
        git(review.HERE, "add", build.VERDICTS_FILE)
        if git(review.HERE, "diff", "--cached", "--name-only"):
            counts_str = ", ".join("%s %d" % (k, v) for k, v in sorted(result["verdict_counts"].items()))
            new_good = result["new_good"]
            git(review.HERE, "commit", "-m", "Item review %s: %d new item%s (%s)"
                % (result["run_id"], len(new_good), "" if len(new_good) == 1 else "s", counts_str or "none"))

    _push_if_ahead(review.HERE, branch)


# ---------------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_prepare = sub.add_parser("prepare", help="sync, select, fetch, batch and write judge views")
    p_prepare.set_defaults(func=cmd_prepare)

    p_finish = sub.add_parser("finish", help="validate judge output, merge, commit and push")
    p_finish.add_argument("--tokens", type=int, default=None,
                           help="token cost for this run, as measured by the caller (optional; "
                                "never invented if omitted)")
    p_finish.add_argument("--tokens-source", default=None,
                           help="how --tokens was measured (required if --tokens is given)")
    p_finish.set_defaults(func=cmd_finish)

    args = parser.parse_args()
    try:
        args.func(args)
    except RuntimeError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
