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


def test_doc_id_for_backup_truncates_to_16():
    long_id = "X" * 44
    assert drift.doc_id_for_backup(long_id) == "X" * 16


def test_doc_id_for_baseline_keeps_full_id():
    long_id = "X" * 44
    assert drift.doc_id_for_baseline(long_id) == long_id


def test_default_backup_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("LIVEDOCS_BACKUP_DIR", str(tmp_path / "custom"))
    assert drift.default_backup_dir() == tmp_path / "custom"


def test_default_backup_dir_falls_back_to_home(monkeypatch):
    monkeypatch.delenv("LIVEDOCS_BACKUP_DIR", raising=False)
    p = drift.default_backup_dir()
    assert str(p).startswith(str(Path.home()))
    assert p.name == "backups"


def test_check_drift_returns_false_when_no_baseline(tmp_path):
    drifted, summary = drift.check_drift("current text", "DOC_ID_1", tmp_path)
    assert drifted is False
    assert "no baseline" in summary


def test_save_and_check_drift_round_trip_no_drift(tmp_path):
    drift.save_last_push("identical content", "DOC_ID_2", tmp_path)
    drifted, summary = drift.check_drift("identical content", "DOC_ID_2", tmp_path)
    assert drifted is False
    assert summary == ""


def test_check_drift_detects_diff(tmp_path):
    drift.save_last_push("line one\nline two\n", "DOC_ID_3", tmp_path)
    drifted, summary = drift.check_drift(
        "line one\nUSER EDIT\nline two\n", "DOC_ID_3", tmp_path
    )
    assert drifted is True
    assert "USER EDIT" in summary


def test_check_drift_truncates_long_diffs(tmp_path):
    base = "\n".join(f"line {i}" for i in range(200))
    current = "\n".join(f"NEW {i}" for i in range(200))
    drift.save_last_push(base, "DOC_ID_4", tmp_path)
    drifted, summary = drift.check_drift(current, "DOC_ID_4", tmp_path, diff_max_lines=20)
    assert drifted is True
    assert "truncated" in summary


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


def test_backup_base_path_uses_short_id(tmp_path):
    doc = "https://docs.google.com/document/d/" + "Z" * 44 + "/edit"
    base = drift.backup_base_path(tmp_path, doc, timestamp="20260101_000000")
    assert base.name == "doc_backup_20260101_000000_" + "Z" * 16
