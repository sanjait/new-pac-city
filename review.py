#!/usr/bin/env python3
"""New PAC City — item-reviewer preparation and merge.

This is the machinery around the LLM reviewer's judgment (plan-item-review.md
step 4, schema v3), not the judgment itself: nothing in this file reads a
story and decides anything about it.

    python3 review.py prep [story-list.json]
        Selects the independent-text class (the schema's mechanical class
        rule — see `classify()`), skips anything already verdicted, fetches
        each remaining item's article body via article.py (one request at a
        time, throttled), computes the fields the schema says the SCRIPT
        computes — byline, extent, lock, evidence_basis — and writes
        review-work/batch-NN.json, at most 25 items each, for a judge to
        read next.

    python3 review.py merge --tokens N --tokens-source "<how measured>"
        Reads every review-work/out-NN.json the judge produced (shape:
        {"verdicts": {"<id>": {...judged fields...}}}), validates each row
        against schema v3, attaches the computed fields carried in the
        batches, and writes verdicts.json (the shape build.read_verdicts and
        build.merge_verdicts expect) plus review-work/hidden-if-live.md.

Stdlib only, matching the rest of this repo. Never judges anything: `prep`
only reads and fetches, `merge` only validates and attaches what the batch
already computed.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import article
import build

HERE = Path(__file__).parent
REVIEW_WORK = HERE / "review-work"
BATCH_SIZE = 25
PROGRAMS_PATH = HERE / "data" / "programs.json"

# Provenance recorded in every run record — see work-verdict-schema.md §2.
SCHEMA_VERSION = "v3"
PROMPT_VERSION = "v3"
MODEL = "claude-sonnet-5"

VALID_VERDICTS = {"publish", "hold", "drop"}
VALID_KINDS = {"column", "preview", "recap", "news"}
VALID_EVIDENCE = {"title-only", "title+summary", "title+full-text"}
VALID_COVERAGE = {"about", "involves"}

READING_WPM = 200


# ------------------------------------------------------------- the class rule
#
# work-verdict-schema.md itself never states this rule in so many words — it
# is stated, verbatim, in plan-item-review.md §2d and plan-schema-v3.md §2d
# ("The schema's mechanical class rule (audio -> `medium`; institutional ->
# source ends in "Athletics" or is "Pac-12 Conference"; independent text ->
# the rest)"), where it is described as a cross-check against the schema and
# against step 1's own test fixture, and both agree with it. That wording is
# reproduced here exactly; see the report for this note.

def classify(item):
    if item.get("medium") == "audio":
        return "audio"
    source = (item.get("source") or "").strip()
    if source.endswith("Athletics") or source == "Pac-12 Conference":
        return "institutional"
    return "independent_text"


def load_registry_keys(path=PROGRAMS_PATH):
    """Every valid `subjects` key: 'conference', each bare school slug (the
    department depth), and each 'school/program' key."""
    data = json.loads(path.read_text(encoding="utf-8"))
    keys = {"conference"}
    for slug, school in data["schools"].items():
        keys.add(slug)
        for program in school["programs"]:
            keys.add(program["key"])
    return keys


def load_all_items(list_path):
    """Every item in the story list, deduped by id, as {id: (item, feed_team)}.
    An id has never been observed under two team buckets in this corpus, but
    dedupe anyway rather than assume that stays true."""
    data = json.loads(list_path.read_text(encoding="utf-8"))
    out = {}
    for team, items in data["teams"].items():
        for it in items:
            out.setdefault(it["id"], (it, team))
    return out


def reading_time(word_count):
    if not word_count:
        return None
    minutes = max(1, round(word_count / READING_WPM))
    return "%d min read" % minutes


# --------------------------------------------------------------------- prep

def cmd_prep(args):
    started = time.monotonic()
    list_path = Path(args.story_list)
    items_by_id = load_all_items(list_path)

    # "Skip items that already carry a verdict in an existing verdicts.json
    # (none exists yet; the code must handle both)." build.read_verdicts
    # already collapses "no file" / "unreadable" / "malformed" to {}.
    existing_verdicts = build.read_verdicts(HERE / build.VERDICTS_FILE)

    selected = [
        (item, feed_team)
        for item_id, (item, feed_team) in items_by_id.items()
        if classify(item) == "independent_text" and item_id not in existing_verdicts
    ]

    REVIEW_WORK.mkdir(exist_ok=True)
    for old in REVIEW_WORK.glob("batch-*.json"):
        old.unlink()

    per_source_selected = {}
    per_source_unreadable = {}
    batch_entries = []

    for item, feed_team in selected:
        source = item.get("source") or "(unknown source)"
        per_source_selected[source] = per_source_selected.get(source, 0) + 1

        link = item.get("link")
        if link:
            result = article.extract(link)
        else:
            result = {"ok": False, "status": None, "error": "item carries no link",
                       "robots_blocked": False, "body": None, "word_count": None,
                       "byline": [], "paywall": None, "paywall_arm": None}

        if not result["ok"]:
            status_key = "%s — %s" % (result.get("status") or "no HTTP status",
                                       result.get("error") or "unknown error")
            per_source_unreadable.setdefault(source, {})
            per_source_unreadable[source][status_key] = \
                per_source_unreadable[source].get(status_key, 0) + 1

        body = result.get("body")
        word_count = result.get("word_count")
        byline = article.merge_bylines(result.get("byline"), item.get("author"))
        lock = result.get("paywall")  # "free" | "paywall" | None

        summary = (item.get("summary") or "").strip() or None
        if body:
            evidence_basis = "title+full-text"
        elif summary:
            evidence_basis = "title+summary"
        else:
            evidence_basis = "title-only"

        shown_to_reviewer = {
            "title": item.get("title"),
            "summary": summary,
            "source": item.get("source"),
            "medium": item.get("medium"),
            "date": item.get("date"),
            "link": link,
            "class": "independent_text",
            "feed_team": feed_team,
            "feed_sport": item.get("sport"),
            "body": body,
            "evidence_basis": evidence_basis,
        }
        computed = {
            "byline": byline,
            "extent": reading_time(word_count) if body else None,
            "fetch": {"ok": result["ok"], "status": result.get("status"),
                      "error": result.get("error")},
        }
        if lock:
            computed["lock"] = lock  # absent, never null, when unknown

        batch_entries.append({
            "id": item["id"],
            "shown_to_reviewer": shown_to_reviewer,
            "computed": computed,
        })

    batches_written = 0
    for i in range(0, len(batch_entries), BATCH_SIZE):
        chunk = batch_entries[i:i + BATCH_SIZE]
        batches_written += 1
        path = REVIEW_WORK / ("batch-%02d.json" % batches_written)
        path.write_text(json.dumps({"items": chunk}, indent=2, ensure_ascii=False),
                         encoding="utf-8")

    wall_clock_s = time.monotonic() - started
    stats = {
        "items_selected": len(batch_entries),
        "batches_written": batches_written,
        "per_source_selected": per_source_selected,
        "per_source_unreadable": per_source_unreadable,
        "wall_clock_prep_s": wall_clock_s,
        "story_list": str(list_path),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    (REVIEW_WORK / "prep-stats.json").write_text(
        json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8")

    print("Items selected: %d" % len(batch_entries))
    print("Batches written: %d" % batches_written)
    print()
    print("Per-source selected:")
    for source, count in sorted(per_source_selected.items(), key=lambda kv: -kv[1]):
        print("  %-32s %d" % (source, count))
    print()
    print("Per-source unreadable (status/error -> count):")
    if not per_source_unreadable:
        print("  none")
    else:
        for source in sorted(per_source_unreadable):
            for status_key, count in sorted(per_source_unreadable[source].items()):
                print("  %-32s %-40s %d" % (source, status_key, count))
    print()
    print("Wall clock: %.1fs" % wall_clock_s)


# -------------------------------------------------------------------- merge

def load_batches():
    """Every batch entry, keyed by id — the only record, before verdicts.json
    exists, of what a judge was shown and what the script computed for it."""
    by_id = {}
    for path in sorted(REVIEW_WORK.glob("batch-*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for entry in data.get("items", []):
            by_id[entry["id"]] = entry
    return by_id


def validate_row(row, registry_keys, batch_entry):
    """Mechanical checks only, against schema v3. Returns (ok, problems) —
    problems lists every failed check, not just the first, so an excluded
    row's file entry says why."""
    if not isinstance(row, dict):
        return False, ["row is not a JSON object"]

    problems = []

    verdict = row.get("verdict")
    if verdict not in VALID_VERDICTS:
        problems.append("verdict %r not one of %s" % (verdict, sorted(VALID_VERDICTS)))

    reason = row.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        problems.append("reason is missing or empty (never optional)")

    evidence_basis = row.get("evidence_basis")
    if evidence_basis not in VALID_EVIDENCE:
        problems.append("evidence_basis %r not one of %s" % (evidence_basis, sorted(VALID_EVIDENCE)))
    elif batch_entry is not None:
        shown = batch_entry["shown_to_reviewer"]["evidence_basis"]
        if evidence_basis != shown:
            problems.append("evidence_basis %r does not match what was actually shown (%r)"
                             % (evidence_basis, shown))

    kind = row.get("kind")
    if kind is not None and kind not in VALID_KINDS:
        problems.append("kind %r not one of %s" % (kind, sorted(VALID_KINDS)))

    subjects = row.get("subjects", {})
    if not isinstance(subjects, dict):
        problems.append("subjects is not an object")
    else:
        for key, level in subjects.items():
            if key not in registry_keys:
                problems.append("subject key %r is not in data/programs.json" % key)
            if level not in VALID_COVERAGE:
                problems.append("subject %r has coverage %r, not about/involves" % (key, level))

    unrecognized = row.get("unrecognized", [])
    if not isinstance(unrecognized, list) or not all(isinstance(x, str) for x in unrecognized):
        problems.append("unrecognized is not a list of strings")

    # The one mechanically-checkable half of "a drop on substance may never
    # rest on title-only evidence" (schema §5). A script cannot tell "the
    # headline itself carries it" apart from "the reason argues substance" —
    # that needs reading the reason's argument, which is exactly the
    # judgment we are not doing here. So this is a conservative proxy: EVERY
    # drop resting on title-only evidence is flagged, which will also catch
    # some legitimate headline-carried drops as false positives. Flagging
    # (and excluding, for a human to re-admit) is the safe direction — see
    # the report's "could not implement mechanically" note.
    if verdict == "drop" and evidence_basis == "title-only":
        problems.append("drop on title-only evidence — cannot mechanically confirm this is a "
                         "headline-carried category drop rather than a substance judgment")

    return (len(problems) == 0), problems


def build_hidden_if_live(good, bad_ids, batches_by_id):
    per_source_reviewed, per_source_hidden = {}, {}
    hidden_items = []
    for item_id, row in good.items():
        entry = batches_by_id.get(item_id)
        source = (entry["shown_to_reviewer"]["source"] if entry else None) or "(unknown source)"
        per_source_reviewed[source] = per_source_reviewed.get(source, 0) + 1
        if row.get("verdict") in ("drop", "hold"):
            per_source_hidden[source] = per_source_hidden.get(source, 0) + 1
            hidden_items.append((source, entry["shown_to_reviewer"]["title"] if entry else item_id, row))

    lines = ["# Hidden if live", ""]

    flagged = [(s, per_source_hidden.get(s, 0), n) for s, n in per_source_reviewed.items()
               if n and per_source_hidden.get(s, 0) / n > (1.0 / 3.0)]
    if flagged:
        lines.append("## Sources losing more than a third of their reviewed items")
        lines.append("")
        for source, hidden, reviewed in sorted(flagged, key=lambda t: -(t[1] / t[2])):
            lines.append("- **%s** — %d/%d (%.0f%%)" % (source, hidden, reviewed, 100.0 * hidden / reviewed))
        lines.append("")

    lines.append("## Per source")
    lines.append("")
    for source in sorted(per_source_reviewed):
        reviewed = per_source_reviewed[source]
        hidden = per_source_hidden.get(source, 0)
        share = (hidden / reviewed) if reviewed else 0.0
        lines.append("- **%s** — reviewed %d, would-hide %d (%.0f%%)"
                     % (source, reviewed, hidden, share * 100.0))
    lines.append("")

    lines.append("## Items that would be hidden")
    lines.append("")
    if not hidden_items:
        lines.append("None.")
    for source, title, row in sorted(hidden_items, key=lambda t: (t[0], t[1] or "")):
        lines.append("- **[%s]** %s" % (row.get("verdict"), title))
        lines.append("  - source: %s" % source)
        lines.append("  - evidence_basis: %s" % row.get("evidence_basis"))
        lines.append("  - reason: %s" % row.get("reason"))
    lines.append("")

    reviewed_ids = set(good.keys())
    no_verdict = [entry for item_id, entry in batches_by_id.items() if item_id not in reviewed_ids]
    lines.append("## Items with no verdict — would be SHOWN")
    lines.append("")
    if not no_verdict:
        lines.append("None — every selected item got a valid verdict.")
    else:
        for entry in sorted(no_verdict,
                             key=lambda e: (e["shown_to_reviewer"]["source"] or "",
                                            e["shown_to_reviewer"]["title"] or "")):
            lines.append("- %s — *%s*" % (entry["shown_to_reviewer"]["title"],
                                          entry["shown_to_reviewer"]["source"]))
    lines.append("")

    return "\n".join(lines)


def merge_outputs(out_paths, batches_by_id, registry_keys):
    """The core of `merge`, split out so the tests can call it directly
    against a temp directory of out-*.json files without going through
    argparse or the filesystem layout in REVIEW_WORK."""
    good, bad = {}, []
    seen_ids = set()
    for path in out_paths:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception as exc:
            print("WARNING: skipping unreadable %s (%s)" % (path, exc), file=sys.stderr)
            continue
        verdicts = data.get("verdicts", {})
        if not isinstance(verdicts, dict):
            print("WARNING: %s carries no usable verdicts object" % path, file=sys.stderr)
            continue
        for item_id, row in verdicts.items():
            if item_id in seen_ids:
                bad.append((item_id, ["duplicate id across out-*.json files — first one kept"]))
                continue
            seen_ids.add(item_id)
            batch_entry = batches_by_id.get(item_id)
            ok, problems = validate_row(row, registry_keys, batch_entry)
            if not ok:
                bad.append((item_id, problems))
                continue
            merged = dict(row)
            if batch_entry is not None:
                computed = batch_entry["computed"]
                if computed.get("byline"):
                    merged["byline"] = computed["byline"]
                if computed.get("extent"):
                    merged["extent"] = computed["extent"]
                if computed.get("lock"):
                    merged["lock"] = computed["lock"]
            good[item_id] = merged
    return good, bad


def cmd_merge(args):
    batches_by_id = load_batches()
    registry_keys = load_registry_keys()
    out_paths = sorted(REVIEW_WORK.glob("out-*.json"))

    good, bad = merge_outputs(out_paths, batches_by_id, registry_keys)

    run_id = "run_%s" % datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M")
    for item_id in good:
        good[item_id]["run"] = run_id

    stats = {}
    stats_path = REVIEW_WORK / "prep-stats.json"
    if stats_path.exists():
        stats = json.loads(stats_path.read_text(encoding="utf-8"))

    verdict_counts = {}
    hidden_per_source = {}
    unrecognized_queue = []
    for item_id, row in good.items():
        v = row.get("verdict")
        verdict_counts[v] = verdict_counts.get(v, 0) + 1
        entry = batches_by_id.get(item_id)
        source = (entry["shown_to_reviewer"]["source"] if entry else None) or "(unknown source)"
        if v in ("drop", "hold"):
            hidden_per_source[source] = hidden_per_source.get(source, 0) + 1
        for phrase in (row.get("unrecognized") or []):
            unrecognized_queue.append({"id": item_id, "source": source, "phrase": phrase})

    run_record = {
        "schema": SCHEMA_VERSION,
        "prompt": PROMPT_VERSION,
        "model": MODEL,
        "started": stats.get("generated_at") or datetime.now(timezone.utc).isoformat(),
        "items": stats.get("items_selected", len(good) + len(bad)),
        "verdict_counts": verdict_counts,
        "hidden_per_source": hidden_per_source,
        "unreadable_per_source": stats.get("per_source_unreadable", {}),
        "unrecognized": unrecognized_queue,
        "bad_rows_excluded": len(bad),
        "wall_clock_prep_s": stats.get("wall_clock_prep_s"),
        # The script cannot measure tokens itself (plan-schema-v3 §6.2's
        # four-hour lesson: an agent's billed total is not the cost of the
        # work). It records only what it is given, and where that number
        # came from.
        "tokens": args.tokens,
        "tokens_source": args.tokens_source,
    }

    out_data = {"verdicts": good, "runs": {run_id: run_record}}
    (HERE / build.VERDICTS_FILE).write_text(
        json.dumps(out_data, indent=2, ensure_ascii=False), encoding="utf-8")

    REVIEW_WORK.mkdir(exist_ok=True)
    (REVIEW_WORK / "hidden-if-live.md").write_text(
        build_hidden_if_live(good, [b[0] for b in bad], batches_by_id), encoding="utf-8")

    print("Merged %d good rows, excluded %d bad rows." % (len(good), len(bad)))
    if bad:
        print()
        print("Excluded rows:")
        for item_id, problems in bad:
            print("  %s: %s" % (item_id, "; ".join(problems)))
    print()
    print("Run record %s:" % run_id)
    print(json.dumps(run_record, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_prep = sub.add_parser("prep", help="select, fetch and batch the independent-text class")
    p_prep.add_argument("story_list", nargs="?", default=str(HERE / "story-list.json"))
    p_prep.set_defaults(func=cmd_prep)

    p_merge = sub.add_parser("merge", help="validate judge output and write verdicts.json")
    p_merge.add_argument("--tokens", type=int, required=True,
                         help="token cost for this run, as measured by the caller")
    p_merge.add_argument("--tokens-source", required=True,
                         help="how --tokens was measured (never an agent's self-reported "
                              "billing total, unlabelled)")
    p_merge.set_defaults(func=cmd_merge)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
