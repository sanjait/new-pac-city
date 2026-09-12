#!/usr/bin/env python3
"""Tests for review.py — plain python, no third-party test runner.

    python3 test_review.py

Three things checked, matching plan-item-review.md step 4's spec:

  1. merge excludes malformed rows without dropping good ones.
  2. the verdicts.json review.py writes is read correctly by
     build.read_verdicts, and build.merge_verdicts matches its ids onto a
     story list.
  3. rendering the site with no verdicts file is byte-identical across two
     separate renders — nothing in this change alters render.py/build.py's
     rendering, and the render is deterministic given the same inputs.
"""
import filecmp
import json
import shutil
import sys
import tempfile
from pathlib import Path

import build
import review

HERE = Path(__file__).parent


def _batch_entry(item_id, source="Test Source", title="A test headline",
                  evidence_basis="title+summary", byline=None, extent=None, lock=None):
    computed = {"byline": byline or [], "extent": extent,
                "fetch": {"ok": True, "status": 200, "error": None}}
    if lock:
        computed["lock"] = lock
    return {
        "id": item_id,
        "shown_to_reviewer": {
            "title": title, "summary": "A test summary.", "source": source,
            "medium": "text", "date": "2026-09-10T00:00:00+00:00",
            "link": "https://example.test/%s" % item_id, "class": "independent_text",
            "feed_team": "Test Team", "feed_sport": "football", "body": None,
            "evidence_basis": evidence_basis,
        },
        "computed": computed,
    }


def test_merge_excludes_malformed_rows_without_dropping_good_rows():
    registry_keys = {"conference", "oregon-state", "oregon-state/football"}
    batches_by_id = {
        "good-1": _batch_entry("good-1", evidence_basis="title+summary",
                                byline=["Jane Smith"], extent="4 min read"),
        "bad-verdict": _batch_entry("bad-verdict", evidence_basis="title+summary"),
        "bad-reason": _batch_entry("bad-reason", evidence_basis="title+summary"),
        "bad-subject": _batch_entry("bad-subject", evidence_basis="title+summary"),
        "good-2": _batch_entry("good-2", evidence_basis="title-only"),
    }

    verdicts_payload = {
        "verdicts": {
            "good-1": {
                "subjects": {"oregon-state/football": "about"}, "unrecognized": [],
                "kind": "news", "verdict": "publish",
                "reason": "A straightforward report on the football program.",
                "evidence_basis": "title+summary",
            },
            "bad-verdict": {
                "subjects": {}, "unrecognized": [], "kind": "news",
                "verdict": "maybe",  # not in publish/hold/drop
                "reason": "Something.", "evidence_basis": "title+summary",
            },
            "bad-reason": {
                "subjects": {}, "unrecognized": [], "kind": "news",
                "verdict": "drop", "reason": "",  # empty, never allowed
                "evidence_basis": "title+summary",
            },
            "bad-subject": {
                "subjects": {"oregon-state/rowing": "about"},  # not in the test registry
                "unrecognized": [], "kind": "news", "verdict": "publish",
                "reason": "About the rowing program.", "evidence_basis": "title+summary",
            },
            "good-2": {
                # v4: a category drop resting on title-only evidence is a
                # WARNING, not an exclusion -- the row is kept as judged
                # (work-verdict-schema.md §5, "no lean in either direction").
                "subjects": {}, "unrecognized": [], "kind": "news",
                "verdict": "drop", "reason": "Season tickets on sale now.",
                "evidence_basis": "title-only",
            },
        }
    }

    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "out-01.json"
        out_path.write_text(json.dumps(verdicts_payload), encoding="utf-8")

        good, bad, warnings = review.merge_outputs([out_path], batches_by_id, registry_keys)

    assert "good-1" in good, "the one fully valid row must survive"
    assert good["good-1"]["byline"] == ["Jane Smith"], "computed byline must be attached"
    assert good["good-1"]["extent"] == "4 min read", "computed extent must be attached"

    bad_ids = {item_id for item_id, _ in bad}
    assert bad_ids == {"bad-verdict", "bad-reason", "bad-subject"}, \
        "only a row whose verdict cannot be trusted may be excluded: %r" % bad_ids
    assert len(good) == 2, "good-2's title-only drop must be KEPT, with a warning, under v4"
    assert "good-2" in good

    warning_ids = {w["id"] for w in warnings}
    assert "good-2" in warning_ids, "the title-only drop must produce a warning"
    print("PASS: test_merge_excludes_malformed_rows_without_dropping_good_rows")


def test_verdicts_json_read_by_build_and_ids_match():
    registry_keys = {"oregon-state/football"}
    batches_by_id = {
        "item-a": _batch_entry("item-a", evidence_basis="title+full-text",
                                byline=["A Writer"], extent="3 min read"),
    }
    verdicts_payload = {
        "verdicts": {
            "item-a": {
                "subjects": {"oregon-state/football": "about"}, "unrecognized": [],
                "kind": "recap", "verdict": "publish",
                "reason": "A gameday recap naming the final score.",
                "evidence_basis": "title+full-text",
            },
        }
    }

    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "out-01.json"
        out_path.write_text(json.dumps(verdicts_payload), encoding="utf-8")
        good, bad, warnings = review.merge_outputs([out_path], batches_by_id, registry_keys)
        assert not bad, "this row is well-formed and must not be excluded: %r" % bad
        assert not warnings, "this row is well-formed and must not carry any warning: %r" % warnings

        run_id = "run_2026-09-11-0000"
        good["item-a"]["run"] = run_id
        verdicts_json_path = Path(tmp) / "verdicts.json"
        verdicts_json_path.write_text(
            json.dumps({"verdicts": good, "runs": {run_id: {"schema": "v3"}}}),
            encoding="utf-8")

        loaded = build.read_verdicts(verdicts_json_path)
        assert set(loaded.keys()) == {"item-a"}, "build.read_verdicts must see the same id"

        by_team = {"conference": [
            {"id": "item-a", "title": "x", "date": None},
            {"id": "item-b", "title": "y", "date": None},
        ]}
        matched, unmatched = build.merge_verdicts(by_team, loaded)
        assert matched == 1 and unmatched == 0, (matched, unmatched)
        assert by_team["conference"][0]["verdict"]["verdict"] == "publish"
        assert "verdict" not in by_team["conference"][1]
    print("PASS: test_verdicts_json_read_by_build_and_ids_match")


def test_unrecognized_object_shape_splits_into_two_kinds():
    registry_keys = {"oregon-state/football"}
    batches_by_id = {
        "item-a": _batch_entry("item-a", evidence_basis="title+summary"),
    }
    verdicts_payload = {
        "verdicts": {
            "item-a": {
                "subjects": {"oregon-state/football": "about"},
                "unrecognized": [
                    {"phrase": "Houston Cougars", "kind": "not-ours"},
                    {"phrase": "club hockey", "kind": "unlisted-program"},
                ],
                "kind": "news", "verdict": "publish",
                "reason": "Names a rival and an unlisted club team in passing.",
                "evidence_basis": "title+summary",
            },
        }
    }
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "out-01.json"
        out_path.write_text(json.dumps(verdicts_payload), encoding="utf-8")
        good, bad, warnings = review.merge_outputs([out_path], batches_by_id, registry_keys)

    assert not bad and not warnings, "a well-formed object-shaped unrecognized list must not fault"
    kinds = {u["phrase"]: u["kind"] for u in good["item-a"]["unrecognized"]}
    assert kinds == {"Houston Cougars": "not-ours", "club hockey": "unlisted-program"}
    print("PASS: test_unrecognized_object_shape_splits_into_two_kinds")


def test_unrecognized_bare_string_keeps_row_with_warning():
    registry_keys = {"oregon-state/football"}
    batches_by_id = {
        "item-a": _batch_entry("item-a", evidence_basis="title+summary"),
    }
    verdicts_payload = {
        "verdicts": {
            "item-a": {
                "subjects": {"oregon-state/football": "about"},
                "unrecognized": ["Houston Cougars"],  # v3-style bare string
                "kind": "news", "verdict": "publish",
                "reason": "Names a rival program in passing.",
                "evidence_basis": "title+summary",
            },
        }
    }
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "out-01.json"
        out_path.write_text(json.dumps(verdicts_payload), encoding="utf-8")
        good, bad, warnings = review.merge_outputs([out_path], batches_by_id, registry_keys)

    assert not bad, "a bare-string unrecognized entry must never invalidate the row: %r" % bad
    assert "item-a" in good
    assert good["item-a"]["unrecognized"] == [{"phrase": "Houston Cougars", "kind": None}], \
        "the bare string is kept, kind unknown: %r" % good["item-a"]["unrecognized"]
    assert any(w["id"] == "item-a" for w in warnings), "a warning must be recorded for the bare string"
    print("PASS: test_unrecognized_bare_string_keeps_row_with_warning")


def test_ambiguous_collected_into_sweep_record():
    registry_keys = {"oregon-state/football"}
    batches_by_id = {
        "item-a": _batch_entry("item-a", source="Test Source", title="Close call headline",
                                evidence_basis="title+summary"),
        "item-b": _batch_entry("item-b", source="Test Source", title="Plain headline",
                                evidence_basis="title+summary"),
    }
    good = {
        "item-a": {"verdict": "publish", "ambiguous": "The subject's sport was not stated outright."},
        "item-b": {"verdict": "publish"},  # no ambiguous field -- must be left untouched
    }
    md, data = review.build_sweep(good, batches_by_id, "run_test")

    assert "item-a" in data["ambiguous_by_source"]["Test Source"][0]["id"] or \
        any(r["id"] == "item-a" for r in data["ambiguous_by_source"]["Test Source"])
    assert list(data["ambiguous_by_source"].keys()) == ["Test Source"]
    assert len(data["ambiguous_by_source"]["Test Source"]) == 1, \
        "a row without ambiguous must not appear in the sweep"
    assert "Close call headline" in md
    assert "Plain headline" not in md
    print("PASS: test_ambiguous_collected_into_sweep_record")


def test_drop_on_title_only_kept_with_warning():
    registry_keys = {"oregon-state/football"}
    batches_by_id = {
        "item-a": _batch_entry("item-a", evidence_basis="title-only"),
    }
    verdicts_payload = {
        "verdicts": {
            "item-a": {
                "subjects": {}, "unrecognized": [], "kind": "news",
                "verdict": "drop", "reason": "The headline names a campus obituary, not athletics.",
                "evidence_basis": "title-only",
            },
        }
    }
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "out-01.json"
        out_path.write_text(json.dumps(verdicts_payload), encoding="utf-8")
        good, bad, warnings = review.merge_outputs([out_path], batches_by_id, registry_keys)

    assert not bad, "a title-only drop must be kept, not excluded, under v4: %r" % bad
    assert "item-a" in good and good["item-a"]["verdict"] == "drop"
    assert any(w["id"] == "item-a" for w in warnings)
    print("PASS: test_drop_on_title_only_kept_with_warning")


def test_bad_verdict_or_unknown_subject_still_excluded():
    registry_keys = {"oregon-state/football"}
    batches_by_id = {
        "bad-verdict": _batch_entry("bad-verdict", evidence_basis="title+summary"),
        "bad-subject": _batch_entry("bad-subject", evidence_basis="title+summary"),
    }
    verdicts_payload = {
        "verdicts": {
            "bad-verdict": {
                "subjects": {}, "unrecognized": [], "kind": "news",
                "verdict": "unsure",  # invalid
                "reason": "Something.", "evidence_basis": "title+summary",
            },
            "bad-subject": {
                "subjects": {"oregon-state/rowing": "about"},  # not in registry
                "unrecognized": [], "kind": "news", "verdict": "publish",
                "reason": "About rowing.", "evidence_basis": "title+summary",
            },
        }
    }
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "out-01.json"
        out_path.write_text(json.dumps(verdicts_payload), encoding="utf-8")
        good, bad, warnings = review.merge_outputs([out_path], batches_by_id, registry_keys)

    bad_ids = {item_id for item_id, _ in bad}
    assert bad_ids == {"bad-verdict", "bad-subject"}
    assert not good
    print("PASS: test_bad_verdict_or_unknown_subject_still_excluded")


def test_sweep_renders_both_headings_when_one_part_empty():
    good = {
        "item-a": {
            "verdict": "publish",
            "unrecognized": [{"phrase": "Houston Cougars", "kind": "not-ours"}],
        },
    }
    batches_by_id = {
        "item-a": _batch_entry("item-a", source="Test Source", title="A rival mentioned in passing"),
    }
    md, data = review.build_sweep(good, batches_by_id, "run_test")

    assert "## Ambiguous items" in md
    assert "## Unlisted names — worth adding? (`unlisted-program`)" in md
    assert "## Unlisted names — simply not covered (`not-ours`)" in md
    # The ambiguous and unlisted-program parts are empty -- each heading must
    # still appear, with a one-line "None." rather than being omitted.
    ambiguous_idx = md.index("## Ambiguous items")
    unlisted_program_idx = md.index("## Unlisted names — worth adding?")
    between = md[ambiguous_idx:unlisted_program_idx]
    assert "None." in between
    assert not data["ambiguous_by_source"]
    assert not data["unlisted"]["unlisted-program"]
    assert "Houston Cougars" in data["unlisted"]["not-ours"]
    print("PASS: test_sweep_renders_both_headings_when_one_part_empty")


def test_render_with_no_verdicts_is_byte_identical():
    cfg = json.loads((HERE / "feeds.json").read_text(encoding="utf-8"))
    list_path = HERE / "story-list.json"
    missing_verdicts_path = HERE / "review-work" / "__no_such_verdicts_file__.json"

    with tempfile.TemporaryDirectory() as tmp:
        out_a = Path(tmp) / "render-a"
        out_b = Path(tmp) / "render-b"

        # build.render always writes under HERE / cfg["output_dir"]. Path's
        # `/` collapses to an absolute right-hand side on both POSIX and
        # Windows, so an absolute output_dir here still lands outside the
        # repo rather than under HERE.
        cfg_a = dict(cfg, output_dir=str(out_a))
        cfg_b = dict(cfg, output_dir=str(out_b))

        build.render(cfg_a, list_path, missing_verdicts_path)
        build.render(cfg_b, list_path, missing_verdicts_path)

        cmp = filecmp.dircmp(out_a, out_b)
        diffs = _collect_diffs(cmp)
        assert not diffs, "renders differ: %r" % diffs
    print("PASS: test_render_with_no_verdicts_is_byte_identical")


def _collect_diffs(cmp):
    diffs = list(cmp.left_only) + list(cmp.right_only) + list(cmp.diff_files)
    for sub in cmp.subdirs.values():
        diffs.extend(_collect_diffs(sub))
    return diffs


def main():
    tests = [
        test_merge_excludes_malformed_rows_without_dropping_good_rows,
        test_verdicts_json_read_by_build_and_ids_match,
        test_unrecognized_object_shape_splits_into_two_kinds,
        test_unrecognized_bare_string_keeps_row_with_warning,
        test_ambiguous_collected_into_sweep_record,
        test_drop_on_title_only_kept_with_warning,
        test_bad_verdict_or_unknown_subject_still_excluded,
        test_sweep_renders_both_headings_when_one_part_empty,
        test_render_with_no_verdicts_is_byte_identical,
    ]
    failures = 0
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failures += 1
            print("FAIL: %s: %s" % (test.__name__, exc))
        except Exception as exc:
            failures += 1
            print("ERROR: %s: %r" % (test.__name__, exc))
    print()
    print("%d passed, %d failed" % (len(tests) - failures, failures))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
