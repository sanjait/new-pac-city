#!/usr/bin/env python3
"""Tests for the hiding behaviour build.py's renderer gained in item-review
plan §8.2 step 5b — plain python, no third-party test runner:

    python3 test_build.py

Five things checked:

  1. with no verdicts file, the rendered site is byte-identical to today's
     (already covered by test_review.py; not repeated here).
  2. with the real verdicts.json, apply_hiding removes exactly the
     drop/hold-verdicted independent-text items and nothing else — audio and
     institutional counts are untouched.
  3. the same holds at the render layer: diffing an unfiltered render against
     the real one, every headline link that disappears traces to one of
     those ids, and nothing is added.
  4. a malformed verdicts file renders the unfiltered site, warning to
     stderr rather than crashing.
  5. a verdict naming an audio or institutional item never hides it, and a
     `hold` verdict hides exactly like `drop`.
  6. every page's "N items" head count agrees with the number of rows
     actually rendered on it.
"""
import filecmp
import html
import io
import json
import re
import sys
import tempfile
from contextlib import redirect_stderr
from pathlib import Path

import build

HERE = Path(__file__).parent
HL_RE = re.compile(r'<a class="hl"[^>]*href="([^"]+)"')
PHEAD_RE = re.compile(r'<p class="phead">.*?·\s*(\d+)\s*item')
ROW_RE = re.compile(r'<article class="row">')


def _load_by_team_with_real_verdicts():
    by_team, _now = build.read_story_list(HERE / "story-list.json")
    verdicts = build.read_verdicts(HERE / "verdicts.json")
    matched, unmatched = build.merge_verdicts(by_team, verdicts)
    return by_team, verdicts, matched, unmatched


def _class_counts(by_team):
    counts = {"audio": 0, "institutional": 0, "independent_text": 0}
    for items in by_team.values():
        for it in items:
            counts[build.classify_for_hiding(it)] += 1
    return counts


def test_apply_hiding_removes_exactly_the_drop_hold_ids():
    by_team, verdicts, matched, unmatched = _load_by_team_with_real_verdicts()
    assert matched > 0, "the real verdicts.json must match items in the real story list"

    expected_hidden_ids = {item_id for item_id, v in verdicts.items()
                            if v.get("verdict") in ("drop", "hold")}
    assert expected_hidden_ids, "the real verdicts.json is expected to carry drop/hold rows"

    before = _class_counts(by_team)
    ids_before = {it["id"] for items in by_team.values() for it in items}

    hidden = build.apply_hiding(by_team)

    ids_after = {it["id"] for items in by_team.values() for it in items}
    after = _class_counts(by_team)

    assert hidden == len(expected_hidden_ids), (hidden, len(expected_hidden_ids))
    assert ids_before - ids_after == expected_hidden_ids, \
        "removed ids must be exactly the drop/hold ids: %r" % (ids_before - ids_after)
    assert not (ids_after - ids_before), "hiding must never add an id"
    assert after["audio"] == before["audio"], "audio count must not change"
    assert after["institutional"] == before["institutional"], "institutional count must not change"
    assert after["independent_text"] == before["independent_text"] - len(expected_hidden_ids)
    print("PASS: test_apply_hiding_removes_exactly_the_drop_hold_ids")


def _render_to(cfg, list_path, verdicts_path, out_dir):
    cfg = dict(cfg, output_dir=str(out_dir))
    build.render(cfg, list_path, verdicts_path)


def _collect_hrefs(out_dir):
    hrefs = set()
    for html_path in out_dir.rglob("index.html"):
        hrefs.update(HL_RE.findall(html_path.read_text(encoding="utf-8")))
    return hrefs


def test_render_diff_only_removes_the_hidden_links():
    cfg = json.loads((HERE / "feeds.json").read_text(encoding="utf-8"))
    list_path = HERE / "story-list.json"
    real_verdicts_path = HERE / "verdicts.json"
    missing_verdicts_path = HERE / "review-work" / "__no_such_verdicts_file__.json"

    verdicts = build.read_verdicts(real_verdicts_path)
    hidden_ids = {item_id for item_id, v in verdicts.items()
                  if v.get("verdict") in ("drop", "hold")}

    by_team, _now = build.read_story_list(list_path)
    id_to_link = {it["id"]: it["link"] for items in by_team.values() for it in items}
    # The renderer HTML-escapes hrefs (html.escape(..., quote=True)) before
    # writing them out, so the raw story-list link needs the same escaping
    # to compare equal to what _collect_hrefs pulled back out of the markup.
    expected_removed_links = {html.escape(id_to_link[i], quote=True)
                               for i in hidden_ids if i in id_to_link}
    assert expected_removed_links, "at least one hidden id must exist in the current story list"

    with tempfile.TemporaryDirectory() as tmp:
        out_unfiltered = Path(tmp) / "unfiltered"
        out_real = Path(tmp) / "real"
        _render_to(cfg, list_path, missing_verdicts_path, out_unfiltered)
        _render_to(cfg, list_path, real_verdicts_path, out_real)

        hrefs_before = _collect_hrefs(out_unfiltered)
        hrefs_after = _collect_hrefs(out_real)

    removed = hrefs_before - hrefs_after
    added = hrefs_after - hrefs_before

    assert not added, "hiding must never add a headline link: %r" % added
    assert removed == expected_removed_links, \
        "removed links must be exactly the hidden items' links: %r" % (removed ^ expected_removed_links)
    print("PASS: test_render_diff_only_removes_the_hidden_links")


def test_malformed_verdicts_renders_unfiltered_with_warning():
    cfg = json.loads((HERE / "feeds.json").read_text(encoding="utf-8"))
    list_path = HERE / "story-list.json"
    missing_verdicts_path = HERE / "review-work" / "__no_such_verdicts_file__.json"

    with tempfile.TemporaryDirectory() as tmp:
        malformed_path = Path(tmp) / "verdicts.json"
        malformed_path.write_text("{not valid json", encoding="utf-8")

        out_clean = Path(tmp) / "clean"
        out_malformed = Path(tmp) / "malformed"

        _render_to(cfg, list_path, missing_verdicts_path, out_clean)

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            _render_to(cfg, list_path, malformed_path, out_malformed)

        assert "WARNING" in stderr.getvalue(), \
            "a malformed verdicts file must warn, not crash: %r" % stderr.getvalue()

        cmp = filecmp.dircmp(out_clean, out_malformed)
        diffs = _collect_diffs(cmp)
        assert not diffs, "a malformed verdicts file must render the unfiltered site: %r" % diffs
    print("PASS: test_malformed_verdicts_renders_unfiltered_with_warning")


def test_verdict_on_audio_or_institutional_item_never_hides_and_hold_hides_like_drop():
    audio_item = {"id": "a1", "medium": "audio", "source": "Some Podcast",
                  "verdict": {"verdict": "drop"}}
    institutional_item = {"id": "i1", "medium": "text", "source": "Oregon State Athletics",
                           "verdict": {"verdict": "hold"}}
    independent_drop = {"id": "t1", "medium": "text", "source": "The Oregonian",
                         "verdict": {"verdict": "drop"}}
    independent_hold = {"id": "t2", "medium": "text", "source": "The Oregonian",
                         "verdict": {"verdict": "hold"}}
    independent_publish = {"id": "t3", "medium": "text", "source": "The Oregonian",
                            "verdict": {"verdict": "publish"}}
    independent_no_verdict = {"id": "t4", "medium": "text", "source": "The Oregonian"}

    assert build.item_is_hidden(audio_item) is False, "audio must never be hidden by a verdict"
    assert build.item_is_hidden(institutional_item) is False, \
        "institutional must never be hidden by a verdict"
    assert build.item_is_hidden(independent_drop) is True
    assert build.item_is_hidden(independent_hold) is True, "hold must hide exactly like drop"
    assert build.item_is_hidden(independent_publish) is False
    assert build.item_is_hidden(independent_no_verdict) is False, "no verdict must always show"
    print("PASS: test_verdict_on_audio_or_institutional_item_never_hides_and_hold_hides_like_drop")


def test_page_and_head_counts_agree_with_rendered_items():
    cfg = json.loads((HERE / "feeds.json").read_text(encoding="utf-8"))
    list_path = HERE / "story-list.json"
    real_verdicts_path = HERE / "verdicts.json"

    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp) / "real"
        _render_to(cfg, list_path, real_verdicts_path, out_dir)

        checked = 0
        for html_path in out_dir.rglob("index.html"):
            text = html_path.read_text(encoding="utf-8")
            m = PHEAD_RE.search(text)
            if not m:
                continue  # /about/ and the policy pages carry no phead
            head_count = int(m.group(1))
            actual_rows = len(ROW_RE.findall(text))
            assert head_count == actual_rows, \
                "%s: head says %d items, %d rows actually rendered" % (
                    html_path, head_count, actual_rows)
            checked += 1
        assert checked > 0, "expected at least one page carrying a phead count"
    print("PASS: test_page_and_head_counts_agree_with_rendered_items (%d pages checked)" % checked)


def _collect_diffs(cmp):
    diffs = list(cmp.left_only) + list(cmp.right_only) + list(cmp.diff_files)
    for sub in cmp.subdirs.values():
        diffs.extend(_collect_diffs(sub))
    return diffs


def main():
    tests = [
        test_apply_hiding_removes_exactly_the_drop_hold_ids,
        test_render_diff_only_removes_the_hidden_links,
        test_malformed_verdicts_renders_unfiltered_with_warning,
        test_verdict_on_audio_or_institutional_item_never_hides_and_hold_hides_like_drop,
        test_page_and_head_counts_agree_with_rendered_items,
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
