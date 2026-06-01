"""Unit tests for the drift module (no browser required)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from livedocs_bridge import drift


def test_extract_doc_id_from_full_url():
    url = "https://docs.google.com/document/d/ABC123_xyz/edit?usp=sharing"
    assert drift.extract_doc_id(url) == "ABC123_xyz"


def test_extract_doc_id_strips_query():
    url = "https://docs.google.com/document/d/ABC123_xyz/?tab=t.0"
    assert drift.extract_doc_id(url) == "ABC123_xyz"


def test_extract_doc_id_returns_none_for_garbage():
    assert drift.extract_doc_id("https://example.com") is None
    assert drift.extract_doc_id("") is None


def test_safe_doc_key_keeps_valid_id_verbatim():
    valid = "ABCdef_123-xyz"
    assert drift.safe_doc_key(valid) == valid


def test_safe_doc_key_extracts_from_url():
    url = "https://docs.google.com/document/d/REAL_ID_42/edit"
    assert drift.safe_doc_key(url) == "REAL_ID_42"


def test_safe_doc_key_hashes_unsafe_input():
    hostile = "../../etc/passwd"
    key = drift.safe_doc_key(hostile)
    assert key.startswith("h_")
    assert "/" not in key and ".." not in key
    assert len(key) == 2 + 32


def test_safe_doc_key_hashes_unicode():
    key = drift.safe_doc_key("doc id with spaces")
    assert key.startswith("h_")


def test_safe_doc_key_none_or_empty():
    assert drift.safe_doc_key(None) == "unknown"
    assert drift.safe_doc_key("") == "unknown"


def test_distinct_doc_ids_sharing_16_char_prefix_do_not_collide():
    # Codex CRITICAL #1: pre-v0.3.1 the 16-char truncation made these collide.
    id_a = "AAAAAAAAAAAAAAAA_first_distinct"
    id_b = "AAAAAAAAAAAAAAAA_second_distinct"
    assert drift.safe_doc_key(id_a) != drift.safe_doc_key(id_b)


def test_legacy_shim_doc_id_for_baseline():
    # Back-compat alias still returns the same safe key.
    assert drift.doc_id_for_baseline("MY_DOC") == drift.safe_doc_key("MY_DOC")
    assert drift.doc_id_for_backup("MY_DOC") == drift.safe_doc_key("MY_DOC")


def test_default_backup_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("LIVEDOCS_BACKUP_DIR", str(tmp_path / "custom"))
    assert drift.default_backup_dir() == tmp_path / "custom"


def test_default_backup_dir_falls_back_to_home(monkeypatch):
    monkeypatch.delenv("LIVEDOCS_BACKUP_DIR", raising=False)
    p = drift.default_backup_dir()
    assert str(p).startswith(str(Path.home()))
    assert p.name == "backups"


def test_check_drift_returns_false_when_no_baseline(tmp_path):
    drifted, summary, meta = drift.check_drift(
        "current text", "DOC_ID_1", tmp_path
    )
    assert drifted is False
    assert "no baseline" in summary
    assert meta == {}


def test_save_and_check_drift_round_trip_no_drift(tmp_path):
    drift.save_last_push("identical content", "DOC_ID_2", tmp_path)
    drifted, summary, meta = drift.check_drift(
        "identical content", "DOC_ID_2", tmp_path
    )
    assert drifted is False
    assert summary == ""
    assert meta == {}


def test_check_drift_detects_diff(tmp_path):
    drift.save_last_push("line one\nline two\n", "DOC_ID_3", tmp_path)
    drifted, summary, meta = drift.check_drift(
        "line one\nUSER EDIT\nline two\n", "DOC_ID_3", tmp_path
    )
    assert drifted is True
    assert "USER EDIT" in summary
    assert meta["hunks_total"] >= 1
    assert meta["truncated"] is False


def test_check_drift_truncates_long_diffs(tmp_path):
    base = "\n".join(f"line {i}" for i in range(200))
    current = "\n".join(f"NEW {i}" for i in range(200))
    drift.save_last_push(base, "DOC_ID_4", tmp_path)
    drifted, summary, meta = drift.check_drift(
        current, "DOC_ID_4", tmp_path, diff_max_lines=20
    )
    assert drifted is True
    # v0.3.4: truncation banner is loud and at the TOP of the summary.
    assert "TRUNCATED" in summary or "truncated" in summary.lower()
    assert meta["truncated"] is True
    assert meta["lines_shown"] == 20
    assert meta["lines_total"] > 20
    # Show count should never exceed total.
    assert meta["hunks_shown"] <= meta["hunks_total"]


def test_prune_old_backups_removes_old(tmp_path):
    old = tmp_path / "doc_backup_20200101_000000_xxx.txt"
    fresh = tmp_path / "doc_backup_20990101_000000_xxx.txt"
    baseline = tmp_path / "_last_pushed_xxx.txt"
    for p in (old, fresh, baseline):
        p.write_text("x")
    # Push `old` mtime back 60 days.
    sixty_days = time.time() - 60 * 86400
    os.utime(old, (sixty_days, sixty_days))

    removed = drift.prune_old_backups(tmp_path, keep_days=30)
    assert removed == 1
    assert not old.exists()
    assert fresh.exists()
    assert baseline.exists()  # baselines are never pruned


def test_prune_old_backups_on_missing_dir(tmp_path):
    missing = tmp_path / "does-not-exist"
    assert drift.prune_old_backups(missing) == 0


def test_list_and_find_backup(tmp_path):
    doc = "https://docs.google.com/document/d/DOC_LIST_ID/edit"
    short = drift.doc_id_for_backup(doc)
    older = tmp_path / f"doc_backup_20200101_000000_{short}.txt"
    older.write_text("older")
    newer_txt = tmp_path / f"doc_backup_20990101_000000_{short}.txt"
    newer_html = tmp_path / f"doc_backup_20990101_000000_{short}.html"
    newer_txt.write_text("newer")
    newer_html.write_text("<div>newer</div>")
    # Make sure the older file actually has an older mtime.
    older_ts = time.time() - 86400 * 365
    os.utime(older, (older_ts, older_ts))

    listed = drift.list_backups(doc, tmp_path)
    assert len(listed) == 2
    assert listed[0]["timestamp"] == "20990101_000000"
    assert listed[1]["timestamp"] == "20200101_000000"

    latest = drift.find_backup(doc, None, tmp_path)
    assert latest["timestamp"] == "20990101_000000"
    assert latest["html"] is not None

    specific = drift.find_backup(doc, "20200101_000000", tmp_path)
    assert specific["timestamp"] == "20200101_000000"

    missing = drift.find_backup(doc, "19000101_000000", tmp_path)
    assert missing is None


def test_resolve_backup_dir_accepts_str_or_path(tmp_path):
    assert drift.resolve_backup_dir(str(tmp_path)) == tmp_path
    assert drift.resolve_backup_dir(tmp_path) == tmp_path


def test_backup_base_path_uses_full_id(tmp_path):
    doc = "https://docs.google.com/document/d/" + "Z" * 44 + "/edit"
    base = drift.backup_base_path(tmp_path, doc, timestamp="20260101_000000")
    assert base.name == "doc_backup_20260101_000000_" + "Z" * 44


def test_atomic_write_text_replaces_existing(tmp_path):
    target = tmp_path / "_last_pushed_ABC.txt"
    target.write_text("OLD", encoding="utf-8")
    drift.atomic_write_text(target, "NEW")
    assert target.read_text(encoding="utf-8") == "NEW"
    # No tmp file should be left behind on success.
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_atomic_write_text_creates_parents(tmp_path):
    nested = tmp_path / "a" / "b" / "c.txt"
    drift.atomic_write_text(nested, "hello")
    assert nested.read_text(encoding="utf-8") == "hello"


def test_list_backups_does_not_mix_two_docs(tmp_path):
    # CRITICAL #1 regression: pre-v0.3.1 these distinct ids shared a 16-char
    # prefix and list_backups returned the other Doc's backups too.
    id_a = "AAAAAAAAAAAAAAAA_first"
    id_b = "AAAAAAAAAAAAAAAA_second"
    a_base = drift.backup_base_path(tmp_path, id_a, timestamp="20260101_000000")
    b_base = drift.backup_base_path(tmp_path, id_b, timestamp="20260102_000000")
    a_base.with_suffix(".txt").write_text("from doc A")
    b_base.with_suffix(".txt").write_text("from doc B")
    a_list = drift.list_backups(id_a, tmp_path)
    b_list = drift.list_backups(id_b, tmp_path)
    assert len(a_list) == 1
    assert len(b_list) == 1
    assert a_list[0]["txt"].read_text() == "from doc A"
    assert b_list[0]["txt"].read_text() == "from doc B"


def test_save_last_push_is_atomic(tmp_path):
    p1 = drift.save_last_push("first", "DOC_ATOMIC", tmp_path)
    # Overwrite — temp file must be cleaned up by os.replace.
    drift.save_last_push("second", "DOC_ATOMIC", tmp_path)
    assert p1.read_text(encoding="utf-8") == "second"
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


# v0.3.4 — Bug 1 regression: drift summary must surface hunk counts so an
# agent isn't tricked into thinking the displayed slice is the whole drift.

def test_check_drift_counts_all_hunks(tmp_path):
    # Build a baseline + current with multiple distinct hunks separated by
    # enough context to render as separate `@@` blocks.
    base = "\n".join(["§1 untouched line " + str(i) for i in range(200)])
    current_lines = ["§1 untouched line " + str(i) for i in range(200)]
    current_lines[10] = "§2 USER EDIT A"
    current_lines[100] = "§4 USER EDIT B"
    current_lines[180] = "§6 USER EDIT C"
    current = "\n".join(current_lines)
    drift.save_last_push(base, "DOC_HUNKS", tmp_path)
    drifted, summary, meta = drift.check_drift(current, "DOC_HUNKS", tmp_path)
    assert drifted is True
    assert meta["hunks_total"] >= 3, (
        f"Expected at least 3 hunks for 3 spread-out edits, got "
        f"hunks_total={meta['hunks_total']}, summary:\n{summary}"
    )


# v0.3.5 — HIGH #1 regression: bound diff materialization for huge inputs.

def test_check_drift_caps_oversized_input_without_running_diff(tmp_path, monkeypatch):
    # Force a tiny input cap so we don't need to allocate 2 MB in a test.
    monkeypatch.setenv("LIVEDOCS_DRIFT_MAX_INPUT_BYTES", "256")
    base = "baseline content here\n" * 50  # ~1.1 KB > 256 B cap
    current = "current content here\n" * 50
    drift.save_last_push(base, "DOC_OVERSIZED", tmp_path)
    drifted, summary, meta = drift.check_drift(current, "DOC_OVERSIZED", tmp_path)
    assert drifted is True
    assert "INPUT TOO LARGE" in summary or "max_input_exceeded" in str(meta)
    assert meta["max_input_exceeded"] is True
    assert meta["baseline_chars"] > 0
    assert meta["current_chars"] > 0


def test_check_drift_stream_caps_at_hard_line_limit(tmp_path, monkeypatch):
    # Force the hard cap low so the test doesn't have to build a 5000-line diff.
    monkeypatch.setattr(drift, "DRIFT_HARD_LINE_CAP", 50)
    base = "\n".join(f"baseline_{i}_unique" for i in range(500))
    current = "\n".join(f"current_{i}_unique" for i in range(500))
    drift.save_last_push(base, "DOC_HARDCAP", tmp_path)
    drifted, summary, meta = drift.check_drift(
        current, "DOC_HARDCAP", tmp_path, diff_max_lines=10
    )
    assert drifted is True
    assert meta["hard_capped"] is True
    assert meta["lines_total"] == 50
    assert "stream-capped" in summary.lower()


def test_drift_max_input_bytes_env_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("LIVEDOCS_DRIFT_MAX_INPUT_BYTES", "garbage")
    assert drift._max_diff_input_bytes() == drift.DEFAULT_DRIFT_MAX_INPUT_BYTES


def test_drift_max_input_bytes_env_negative_falls_back(monkeypatch):
    monkeypatch.setenv("LIVEDOCS_DRIFT_MAX_INPUT_BYTES", "-1")
    assert drift._max_diff_input_bytes() == drift.DEFAULT_DRIFT_MAX_INPUT_BYTES


def test_check_drift_truncation_warning_is_at_top(tmp_path):
    # Verify the loud truncation banner is BEFORE the diff body — an agent
    # reading top-down should hit the "force=True overwrites ALL drift"
    # warning before being lulled by a small-looking diff slice.
    base = "\n".join(f"line {i}" for i in range(500))
    current = "\n".join(f"NEW {i}" for i in range(500))
    drift.save_last_push(base, "DOC_TRUNC", tmp_path)
    _, summary, meta = drift.check_drift(
        current, "DOC_TRUNC", tmp_path, diff_max_lines=30
    )
    first_line = summary.splitlines()[0]
    assert "TRUNCATED" in first_line
    assert "force=True" in first_line.lower() or "force" in summary.splitlines()[0].lower()
    assert meta["truncated"] is True


def test_atomic_write_uses_unique_tmp_filename(tmp_path, monkeypatch):
    # v0.3.2: deterministic `<target>.tmp` was a write race. mkstemp gives
    # every writer its own tmp basename, so concurrent writers can't clobber
    # each other's temp file mid-flight. We assert two things:
    #   1. The tmp filename includes random bytes (not a fixed `.tmp` suffix
    #      next to the target).
    #   2. Repeated writes still end up at the target and leave no stragglers.
    target = tmp_path / "_last_pushed_race.txt"

    captured_tmp_names: list[str] = []
    real_mkstemp = drift.tempfile.mkstemp

    def spy(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        captured_tmp_names.append(Path(name).name)
        return fd, name

    monkeypatch.setattr(drift.tempfile, "mkstemp", spy)

    for i in range(5):
        drift.atomic_write_text(target, f"v{i}")
    assert target.read_text(encoding="utf-8") == "v4"
    # All tmp basenames must differ — deterministic `<target>.tmp` would
    # produce the same string each time.
    assert len(set(captured_tmp_names)) == len(captured_tmp_names)
    # No tmp leftovers post-write.
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_atomic_write_cleans_tmp_on_failure(tmp_path, monkeypatch):
    target = tmp_path / "destination.txt"

    def boom(*a, **k):
        raise RuntimeError("simulated replace failure")

    monkeypatch.setattr(drift.os, "replace", boom)
    with pytest.raises(RuntimeError):
        drift.atomic_write_text(target, "data")
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
