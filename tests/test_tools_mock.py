"""Mock-based smoke tests for tool functions.

These tests patch `BrowserSession` and the helper functions so we exercise
the tool wrappers' control flow (success path, error path, content_type
validation, v0.3 drift logic) without needing a live Chrome.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from livedocs_bridge import drift as drift_mod
from livedocs_bridge import tools


class _FakePage:
    def __init__(self, url="https://docs.google.com/document/d/FAKE_DOC_ID_1234567890abcdef/edit"):
        self.url = url


@asynccontextmanager
async def _fake_session():
    s = MagicMock()
    s.grant_clipboard = AsyncMock()
    yield s


def _patch_common(
    monkeypatch,
    tmp_path: Path,
    *,
    paste_status="CLIP_OK",
    title="Untitled",
    pre_text: str = "",
    post_text: str = "post-paste content",
):
    """Set up a fake browser + drift backup dir under tmp_path.

    `pre_text` is what backup_doc reads (i.e. the Doc's pre-clear state).
    `post_text` is what capture_doc_plain returns after the paste.
    """
    page = _FakePage()
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    monkeypatch.setattr(drift_mod, "default_backup_dir", lambda: backup_dir)

    monkeypatch.setattr(tools, "BrowserSession", lambda *a, **k: _fake_session())
    monkeypatch.setattr(tools, "find_or_open_doc", AsyncMock(return_value=page))
    monkeypatch.setattr(tools, "get_docs_editor", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(tools, "clear_doc", AsyncMock())
    monkeypatch.setattr(tools, "move_caret_to_end", AsyncMock())
    monkeypatch.setattr(tools, "paste_html", AsyncMock(return_value=paste_status))
    monkeypatch.setattr(tools, "insert_text", AsyncMock())
    monkeypatch.setattr(tools, "get_doc_title", AsyncMock(return_value=title))
    monkeypatch.setattr(tools, "get_doc_text", AsyncMock(return_value="hello world"))
    monkeypatch.setattr(tools, "scroll_doc", AsyncMock())
    monkeypatch.setattr(tools, "capture_doc_plain", AsyncMock(return_value=post_text))

    async def fake_backup(editor, dirpath, doc_url_or_id=None):
        target = doc_url_or_id or page.url
        base = drift_mod.backup_base_path(Path(dirpath), target)
        txt_path = base.with_suffix(".txt")
        txt_path.write_text(pre_text, encoding="utf-8")
        html_path = base.with_suffix(".html")
        html_path.write_text(f"<div>{pre_text}</div>", encoding="utf-8")
        return {
            "txt": txt_path,
            "html": html_path,
            "doc_url": page.url,
            "doc_id": drift_mod.extract_doc_id(target),
            "warning": None,
            "error": None,
        }

    monkeypatch.setattr(tools, "backup_doc", fake_backup)
    return page, backup_dir


async def test_docs_open_success(monkeypatch, tmp_path):
    _patch_common(monkeypatch, tmp_path, title="My Memo")
    res = await tools.docs_open("https://docs.google.com/document/d/FAKE/edit")
    assert res["success"] is True
    assert res["title"] == "My Memo"


async def test_docs_replace_all_markdown_first_inject(monkeypatch, tmp_path):
    _patch_common(monkeypatch, tmp_path, pre_text="")
    res = await tools.docs_replace_all("# Hi\n\nbody", "markdown")
    assert res["success"] is True
    assert res["drift_detected"] is False  # no baseline = first inject
    assert res["baseline_saved"] is True
    assert res["chars_injected"] == len("# Hi\n\nbody")
    assert res["backup_paths"]["txt"] is not None


async def test_docs_replace_all_aborts_on_drift(monkeypatch, tmp_path):
    page, backup_dir = _patch_common(
        monkeypatch, tmp_path, pre_text="user edited this"
    )
    # Seed a baseline that disagrees with the current Doc text.
    doc_id = drift_mod.extract_doc_id(page.url)
    drift_mod.save_last_push("the agent's last push", doc_id, backup_dir)

    res = await tools.docs_replace_all("# new", "markdown")
    assert res["success"] is False
    assert res["drift_detected"] is True
    assert "drift_summary" in res
    assert res["backup_paths"]["txt"] is not None
    # paste_html should NOT have been called when we aborted on drift.
    tools.paste_html.assert_not_awaited()  # type: ignore[attr-defined]


async def test_docs_replace_all_force_overrides_drift(monkeypatch, tmp_path):
    page, backup_dir = _patch_common(
        monkeypatch, tmp_path, pre_text="user edited this", post_text="new content"
    )
    doc_id = drift_mod.extract_doc_id(page.url)
    drift_mod.save_last_push("baseline", doc_id, backup_dir)

    res = await tools.docs_replace_all("# new", "markdown", force=True)
    assert res["success"] is True
    assert res["drift_detected"] is True
    assert res["forced"] is True
    # Baseline should now match the post-paste content.
    new_baseline = drift_mod.last_push_path(backup_dir, doc_id).read_text()
    assert new_baseline == "new content"


async def test_docs_replace_all_doc_url_passed_through(monkeypatch, tmp_path):
    _patch_common(monkeypatch, tmp_path)
    pin = "https://docs.google.com/document/d/PINNED_ID/edit"
    await tools.docs_replace_all("# x", "markdown", doc_url=pin)
    tools.find_or_open_doc.assert_awaited()  # type: ignore[attr-defined]
    args, _ = tools.find_or_open_doc.await_args  # type: ignore[attr-defined]
    assert pin in args


async def test_docs_replace_all_propagates_clipboard_failure(monkeypatch, tmp_path):
    _patch_common(
        monkeypatch,
        tmp_path,
        paste_status="CLIP_ERR NotAllowedError: focus lost",
    )
    res = await tools.docs_replace_all("# hi", "markdown")
    assert res["success"] is False
    assert "CLIP_ERR" in res["error"]
    assert res["backup_paths"]["txt"] is not None


async def test_docs_replace_all_rejects_unknown_content_type(monkeypatch, tmp_path):
    _patch_common(monkeypatch, tmp_path)
    res = await tools.docs_replace_all("hi", "rtf")
    assert res["success"] is False
    assert "content_type" in res["error"].lower()


async def test_docs_append_snapshots_and_succeeds(monkeypatch, tmp_path):
    _patch_common(monkeypatch, tmp_path)
    res = await tools.docs_append("more text", "markdown")
    assert res["success"] is True
    assert res["chars_appended"] == len("more text")
    assert res["backup_paths"]["txt"] is not None


async def test_docs_append_falls_back_to_insert_text(monkeypatch, tmp_path):
    _patch_common(monkeypatch, tmp_path, paste_status="CLIP_ERR")
    res = await tools.docs_append("more", "markdown")
    assert res["success"] is True
    tools.insert_text.assert_awaited()  # type: ignore[attr-defined]


async def test_docs_check_drift_no_baseline(monkeypatch, tmp_path):
    _patch_common(monkeypatch, tmp_path, post_text="current doc text")
    res = await tools.docs_check_drift()
    assert res["success"] is True
    assert res["drifted"] is False  # first inject path
    assert res["baseline_exists"] is False


async def test_docs_check_drift_detects_diff(monkeypatch, tmp_path):
    page, backup_dir = _patch_common(
        monkeypatch, tmp_path, post_text="current text after user edit"
    )
    doc_id = drift_mod.extract_doc_id(page.url)
    drift_mod.save_last_push("original baseline", doc_id, backup_dir)
    res = await tools.docs_check_drift()
    assert res["success"] is True
    assert res["drifted"] is True
    assert res["baseline_exists"] is True
    assert "drift_summary" in res and res["drift_summary"]


async def test_docs_restore_from_backup_uses_latest(monkeypatch, tmp_path):
    page, backup_dir = _patch_common(monkeypatch, tmp_path, post_text="restored")
    doc_id = drift_mod.extract_doc_id(page.url)
    base = drift_mod.backup_base_path(backup_dir, doc_id, timestamp="20260101_000000")
    base.with_suffix(".txt").write_text("backup text", encoding="utf-8")
    base.with_suffix(".html").write_text(
        "<div>backup html</div>", encoding="utf-8"
    )
    res = await tools.docs_restore_from_backup(doc_url=page.url)
    assert res["success"] is True
    assert res["restored_timestamp"] == "20260101_000000"
    # Baseline should be updated to post-restore content.
    new_baseline = drift_mod.last_push_path(backup_dir, doc_id).read_text()
    assert new_baseline == "restored"


async def test_docs_restore_from_backup_no_backup(monkeypatch, tmp_path):
    _patch_common(monkeypatch, tmp_path)
    res = await tools.docs_restore_from_backup()
    assert res["success"] is False
    assert "no backup" in res["error"].lower()


async def test_docs_find_replace_empty_find_rejected(monkeypatch):
    res = await tools.docs_find_replace("", "anything", True)
    assert res["success"] is False
    assert "non-empty" in res["error"]


async def test_docs_screenshot_invalid_scroll(monkeypatch):
    res = await tools.docs_screenshot("sideways", None)
    assert res["success"] is False
    assert "scroll_to" in res["error"]


def test_resolve_content_html_passthrough():
    html, n = tools._resolve_content("<b>x</b>", "html")
    assert html == "<b>x</b>"
    assert n == "8"


def test_resolve_content_markdown_to_html():
    html, n = tools._resolve_content("# h", "markdown")
    assert "<h1>h</h1>" in html
    assert n == "3"


def test_resolve_content_rejects_unknown():
    with pytest.raises(ValueError):
        tools._resolve_content("x", "pdf")
