#!/usr/bin/env python3
"""Tests for review_cycle.py — plain python, no third-party test runner.

    python3 test_review_cycle.py

Covers the six cases plan-item-review.md §8.7's runner spec asks for,
plus the git-behaviour cases against a throwaway temp repo. NEVER pushes —
tests that touch git test `sync_with_origin` and the dirty-file refusal
only, never `cmd_finish`'s own git add/commit/push (that half is exercised
only by the attended run, on this machine, watched).

Every test that touches review.HERE / review.REVIEW_WORK monkeypatches both
module attributes to a temp directory and restores them in `finally` —
review.py's own functions read those names from its module globals at call
time, so this redirects review.cmd_prep, review.load_batches, etc. without
touching the real repo.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest.mock as mock
from pathlib import Path

import build
import review
import review_cycle

HERE = Path(__file__).parent


# ------------------------------------------------------------------ fixtures

def _story_list(items_by_team):
    return {"generated_at": "2026-09-11T00:00:00+00:00", "teams": items_by_team}


def _item(item_id, source="The Test Gazette", title="A test headline",
          medium="text", summary="A short summary.", sport=None):
    """An independent-text item with NO `link` — review.cmd_prep only hits
    the network when an item carries a link, so a linkless fixture lets
    `prepare` run for real, end to end, without ever touching the network."""
    return {"id": item_id, "title": title, "source": source, "medium": medium,
            "summary": summary, "author": None, "sport": sport,
            "date": "2026-09-10T00:00:00+00:00"}


class _redirected:
    """Point review.HERE / review.REVIEW_WORK at a temp directory for the
    duration of the block, and put review_cycle's own file reads (which all
    go through review.HERE/review.REVIEW_WORK, never a cached copy) there
    too. Restores both on exit, success or failure."""

    def __init__(self, root):
        self.root = root

    def __enter__(self):
        self._orig_here = review.HERE
        self._orig_work = review.REVIEW_WORK
        review.HERE = self.root
        review.REVIEW_WORK = self.root / "review-work"
        return self.root

    def __exit__(self, *exc):
        review.HERE = self._orig_here
        review.REVIEW_WORK = self._orig_work


def _write_story_list(root, story_list):
    (root / "story-list.json").write_text(json.dumps(story_list), encoding="utf-8")


def _write_verdicts(root, verdicts, runs=None):
    (root / "verdicts.json").write_text(
        json.dumps({"verdicts": verdicts, "runs": runs or {}}), encoding="utf-8")


# --------------------------------------------------------- prepare: no-op

def test_prepare_with_everything_judged_writes_no_batch():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        items = {"boise-state": [_item("id-1"), _item("id-2")]}
        _write_story_list(root, _story_list(items))
        _write_verdicts(root, {
            "id-1": {"verdict": "publish"}, "id-2": {"verdict": "drop"},
        })
        with _redirected(root):
            review.REVIEW_WORK.mkdir(exist_ok=True)
            review_cycle.prepare_body()
            assert not list(review.REVIEW_WORK.glob("batch-*.json")), \
                "no batch should be written when every item already carries a verdict"
            assert not list(review.REVIEW_WORK.glob("view-*.txt"))
    print("PASS: test_prepare_with_everything_judged_writes_no_batch")


# ------------------------------------------------- prepare: selection is right

def test_prepare_selects_only_unjudged_independent_text():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        items = {
            "boise-state": [
                _item("unjudged-text", source="Boise State Beat"),      # selected
                _item("judged-text", source="Boise State Beat"),        # already verdicted
            ],
            "gonzaga": [
                _item("audio-item", source="The Zag Pod", medium="audio"),        # wrong class
            ],
            "conference": [
                _item("institutional-item", source="Boise State Athletics"),      # wrong class
                _item("pac12-item", source="Pac-12 Conference"),                  # wrong class
            ],
        }
        _write_story_list(root, _story_list(items))
        _write_verdicts(root, {"judged-text": {"verdict": "publish"}})
        with _redirected(root):
            review.REVIEW_WORK.mkdir(exist_ok=True)
            review_cycle.prepare_body()

            batch_files = sorted(review.REVIEW_WORK.glob("batch-*.json"))
            assert len(batch_files) == 1
            data = json.loads(batch_files[0].read_text(encoding="utf-8"))
            ids = {entry["id"] for entry in data["items"]}
            assert ids == {"unjudged-text"}, \
                "must select exactly the one unjudged independent-text item: %r" % ids

            view_files = sorted(review.REVIEW_WORK.glob("view-*.txt"))
            assert len(view_files) == 1
            assert "unjudged-text" in view_files[0].read_text(encoding="utf-8")
    print("PASS: test_prepare_selects_only_unjudged_independent_text")


# ------------------------------------------------------- the judge view shape

def test_judge_view_caps_long_body_and_marks_the_cut():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        long_body = "word " * 4000  # ~20,000 characters, over the 15,000 cap
        batch = {
            "items": [{
                "id": "long-item",
                "shown_to_reviewer": {
                    "title": "A very long story", "summary": "Short summary.",
                    "source": "Test Source", "medium": "text", "date": "2026-09-10T00:00:00+00:00",
                    "link": "https://example.test/long-item", "class": "independent_text",
                    "feed_team": "Test Team", "feed_sport": None, "body": long_body,
                    "evidence_basis": "title+full-text",
                },
                "computed": {},
            }],
        }
        batch_path = root / "batch-01.json"
        batch_path.write_text(json.dumps(batch), encoding="utf-8")
        view_path = root / "view-01.txt"

        review_cycle.write_judge_view(batch_path, view_path)
        text = view_path.read_text(encoding="utf-8")

        assert "CUT" in text, "the cut must be marked, not silent"
        assert str(review_cycle.BODY_CHAR_CAP) in text
        # The body shown must not exceed the cap plus the wrapping's own
        # newlines (wrapping only ever inserts line breaks, never new text).
        body_marker = text.index("body (")
        cut_marker = text.index("[... CUT")
        shown_body_region = text[body_marker:cut_marker]
        assert len(shown_body_region.replace("\n", "")) <= review_cycle.BODY_CHAR_CAP + 200

        lines = text.splitlines()
        longest = max((len(ln) for ln in lines), default=0)
        assert longest <= 200, "every line must be short enough for a line-oriented tool: %d" % longest
    print("PASS: test_judge_view_caps_long_body_and_marks_the_cut")


def test_judge_view_leaves_short_body_uncut():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        short_body = "This is a short article body about a football game."
        batch = {
            "items": [{
                "id": "short-item",
                "shown_to_reviewer": {
                    "title": "A short story", "summary": "Short summary.",
                    "source": "Test Source", "medium": "text", "date": "2026-09-10T00:00:00+00:00",
                    "link": "https://example.test/short-item", "class": "independent_text",
                    "feed_team": "Test Team", "feed_sport": "football", "body": short_body,
                    "evidence_basis": "title+full-text",
                },
                "computed": {},
            }],
        }
        batch_path = root / "batch-01.json"
        batch_path.write_text(json.dumps(batch), encoding="utf-8")
        view_path = root / "view-01.txt"
        review_cycle.write_judge_view(batch_path, view_path)
        text = view_path.read_text(encoding="utf-8")
        assert "CUT" not in text
        assert "football game" in text
    print("PASS: test_judge_view_leaves_short_body_uncut")


# --------------------------------------------------------------- finish: merge

def _out_file(root, name, verdicts):
    (root / "review-work" / name).write_text(json.dumps({"verdicts": verdicts}), encoding="utf-8")


def _minimal_batches_by_id(root, item_ids):
    entries = []
    for item_id in item_ids:
        entries.append({
            "id": item_id,
            "shown_to_reviewer": {
                "title": "Headline for %s" % item_id, "summary": "s", "source": "Test Source",
                "medium": "text", "date": "2026-09-10T00:00:00+00:00", "link": None,
                "class": "independent_text", "feed_team": "Test Team", "feed_sport": None,
                "body": None, "evidence_basis": "title+summary",
            },
            "computed": {"byline": [], "extent": None, "fetch": {"ok": True, "status": 200, "error": None}},
        })
    (root / "review-work" / "batch-01.json").write_text(
        json.dumps({"items": entries}), encoding="utf-8")


def test_finish_appends_without_altering_an_existing_row():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "review-work").mkdir()
        _write_verdicts(root, {
            "old-item": {"verdict": "publish", "reason": "Was already here.",
                         "evidence_basis": "title+summary", "run": "run_old"},
        }, runs={"run_old": {"schema": "v4"}})
        _minimal_batches_by_id(root, ["new-item"])
        _out_file(root, "out-01.json", {
            "new-item": {
                "subjects": {}, "unrecognized": [], "kind": "news", "verdict": "publish",
                "reason": "A new item.", "evidence_basis": "title+summary",
            },
        })

        with _redirected(root):
            result = review_cycle.merge_and_write(_ns(tokens=None, tokens_source=None))
            assert result is not None
            data = json.loads((root / "verdicts.json").read_text(encoding="utf-8"))
            assert data["verdicts"]["old-item"]["reason"] == "Was already here.", \
                "an existing row must never be rewritten"
            assert data["verdicts"]["old-item"]["run"] == "run_old"
            assert "new-item" in data["verdicts"]
            assert "run_old" in data["runs"] and result["run_id"] in data["runs"]
    print("PASS: test_finish_appends_without_altering_an_existing_row")


class _ns:
    def __init__(self, **kw):
        self.__dict__.update(kw)


# ------------------------------------------------------------ finish: no-op

def test_finish_twice_in_a_row_is_a_noop_the_second_time():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "review-work").mkdir()
        _minimal_batches_by_id(root, ["item-1"])
        _out_file(root, "out-01.json", {
            "item-1": {
                "subjects": {}, "unrecognized": [], "kind": "news", "verdict": "publish",
                "reason": "First run.", "evidence_basis": "title+summary",
            },
        })

        with _redirected(root):
            first = review_cycle.merge_and_write(_ns(tokens=None, tokens_source=None))
            assert first is not None
            after_first = (root / "verdicts.json").read_text(encoding="utf-8")

            second = review_cycle.merge_and_write(_ns(tokens=None, tokens_source=None))
            assert second is None, "a second finish with nothing new must be a no-op"
            after_second = (root / "verdicts.json").read_text(encoding="utf-8")
            assert after_first == after_second, "verdicts.json must not change on the no-op run"
    print("PASS: test_finish_twice_in_a_row_is_a_noop_the_second_time")


# --------------------------------------------------------- finish: push refusal

def _git(repo, *args):
    return subprocess.run(("git", "-C", str(repo)) + args, capture_output=True, text=True,
                           check=True).stdout


def test_finish_refuses_to_push_when_unrelated_file_is_dirty():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "config", "user.email", "test@example.test")
        _git(repo, "config", "user.name", "Test")
        (repo / "other.txt").write_text("original\n", encoding="utf-8")
        (repo / "verdicts.json").write_text("{}\n", encoding="utf-8")
        _git(repo, "add", "other.txt", "verdicts.json")
        _git(repo, "commit", "-q", "-m", "initial")

        # Dirty an unrelated, tracked file -- never touching git push.
        (repo / "other.txt").write_text("changed\n", encoding="utf-8")

        try:
            review_cycle.check_no_foreign_dirt(repo)
            raised = False
        except RuntimeError as exc:
            raised = True
            assert "other.txt" in str(exc), "the refusal must name the offending file: %s" % exc
        assert raised, "must refuse when a file outside verdicts.json is dirty"
    print("PASS: test_finish_refuses_to_push_when_unrelated_file_is_dirty")


def test_finish_does_not_refuse_when_only_verdicts_json_is_dirty():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "config", "user.email", "test@example.test")
        _git(repo, "config", "user.name", "Test")
        (repo / "verdicts.json").write_text("{}\n", encoding="utf-8")
        _git(repo, "add", "verdicts.json")
        _git(repo, "commit", "-q", "-m", "initial")
        (repo / "verdicts.json").write_text('{"verdicts": {}, "runs": {}}\n', encoding="utf-8")

        review_cycle.check_no_foreign_dirt(repo)  # must not raise
    print("PASS: test_finish_does_not_refuse_when_only_verdicts_json_is_dirty")


# --------------------------------------------------------------- sync_with_origin

def test_sync_with_origin_fast_forwards_a_clean_behind_branch():
    with tempfile.TemporaryDirectory() as tmp:
        origin = Path(tmp) / "origin.git"
        clone = Path(tmp) / "clone"
        _git(Path(tmp), "init", "-q", "--bare", "-b", "main", str(origin))

        seed = Path(tmp) / "seed"
        _git(Path(tmp), "clone", "-q", str(origin), str(seed))
        _git(seed, "config", "user.email", "test@example.test")
        _git(seed, "config", "user.name", "Test")
        (seed / "f.txt").write_text("one\n", encoding="utf-8")
        _git(seed, "add", "f.txt")
        _git(seed, "commit", "-q", "-m", "one")
        _git(seed, "push", "-q", "origin", "main")

        _git(Path(tmp), "clone", "-q", str(origin), str(clone))

        # Advance origin again, from the seed checkout -- the clone must not
        # see this commit until sync_with_origin fetches it.
        (seed / "f.txt").write_text("two\n", encoding="utf-8")
        _git(seed, "add", "f.txt")
        _git(seed, "commit", "-q", "-m", "two")
        _git(seed, "push", "-q", "origin", "main")

        branch = review_cycle.sync_with_origin(clone)
        assert branch == "main"
        assert (clone / "f.txt").read_text(encoding="utf-8") == "two\n", \
            "sync_with_origin must fast-forward to origin's latest commit"
    print("PASS: test_sync_with_origin_fast_forwards_a_clean_behind_branch")


def test_sync_with_origin_refuses_a_dirty_tree():
    with tempfile.TemporaryDirectory() as tmp:
        origin = Path(tmp) / "origin.git"
        clone = Path(tmp) / "clone"
        _git(Path(tmp), "init", "-q", "--bare", "-b", "main", str(origin))
        seed = Path(tmp) / "seed"
        _git(Path(tmp), "clone", "-q", str(origin), str(seed))
        _git(seed, "config", "user.email", "test@example.test")
        _git(seed, "config", "user.name", "Test")
        (seed / "f.txt").write_text("one\n", encoding="utf-8")
        _git(seed, "add", "f.txt")
        _git(seed, "commit", "-q", "-m", "one")
        _git(seed, "push", "-q", "origin", "main")
        _git(Path(tmp), "clone", "-q", str(origin), str(clone))

        (clone / "f.txt").write_text("dirty\n", encoding="utf-8")

        try:
            review_cycle.sync_with_origin(clone)
            raised = False
        except RuntimeError:
            raised = True
        assert raised, "must refuse to sync a dirty working tree"
    print("PASS: test_sync_with_origin_refuses_a_dirty_tree")


def test_sync_with_origin_refuses_a_diverged_branch_never_forcing():
    with tempfile.TemporaryDirectory() as tmp:
        origin = Path(tmp) / "origin.git"
        clone = Path(tmp) / "clone"
        _git(Path(tmp), "init", "-q", "--bare", "-b", "main", str(origin))
        seed = Path(tmp) / "seed"
        _git(Path(tmp), "clone", "-q", str(origin), str(seed))
        _git(seed, "config", "user.email", "test@example.test")
        _git(seed, "config", "user.name", "Test")
        (seed / "f.txt").write_text("one\n", encoding="utf-8")
        _git(seed, "add", "f.txt")
        _git(seed, "commit", "-q", "-m", "one")
        _git(seed, "push", "-q", "origin", "main")
        _git(Path(tmp), "clone", "-q", str(origin), str(clone))

        # Origin moves on...
        (seed / "f.txt").write_text("two\n", encoding="utf-8")
        _git(seed, "add", "f.txt")
        _git(seed, "commit", "-q", "-m", "two")
        _git(seed, "push", "-q", "origin", "main")

        # ...and so does the clone, locally, never pushed -- a real divergence.
        _git(clone, "config", "user.email", "test@example.test")
        _git(clone, "config", "user.name", "Test")
        (clone / "g.txt").write_text("local\n", encoding="utf-8")
        _git(clone, "add", "g.txt")
        _git(clone, "commit", "-q", "-m", "local, unpushed")
        before = _git(clone, "rev-parse", "HEAD")

        try:
            review_cycle.sync_with_origin(clone)
            raised = False
        except RuntimeError:
            raised = True
        assert raised, "a diverged branch must be refused, never reset or forced past"
        after = _git(clone, "rev-parse", "HEAD")
        assert before == after, "the local commit must survive the refusal untouched"
    print("PASS: test_sync_with_origin_refuses_a_diverged_branch_never_forcing")


def main():
    tests = [
        test_prepare_with_everything_judged_writes_no_batch,
        test_prepare_selects_only_unjudged_independent_text,
        test_judge_view_caps_long_body_and_marks_the_cut,
        test_judge_view_leaves_short_body_uncut,
        test_finish_appends_without_altering_an_existing_row,
        test_finish_twice_in_a_row_is_a_noop_the_second_time,
        test_finish_refuses_to_push_when_unrelated_file_is_dirty,
        test_finish_does_not_refuse_when_only_verdicts_json_is_dirty,
        test_sync_with_origin_fast_forwards_a_clean_behind_branch,
        test_sync_with_origin_refuses_a_dirty_tree,
        test_sync_with_origin_refuses_a_diverged_branch_never_forcing,
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
