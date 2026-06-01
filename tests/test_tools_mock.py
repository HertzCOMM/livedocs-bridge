"""Mock-based smoke tests for tool functions.

These tests patch `BrowserSession` and the helper functions so we exercise
the tool wrappers' control flow (success path, error path, content_type
validation, v0.3 drift logic) without needing a live Chrome.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
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
    recapture_text: Optional[str] = None,
    backup_capture_failed: bool = False,
):
    """Set up a fake browser + drift backup dir under tmp_path.

    Args:
        pre_text: what backup_doc reads (i.e. the Doc's pre-clear state).
        post_text: what capture_doc_plain returns AFTER the paste.
        recapture_text: when set, simulates a TOCTOU divergence — the
            pre-clear recapture returns this text instead of `pre_text`.
        backup_capture_failed: when True, the fake `backup_doc` returns no
            txt/html (simulating a clipboard read failure).
    """
    page = _FakePage()
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(exist_ok=True)
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

    # capture_doc_plain is called twice during docs_replace_all:
    #   1) pre-clear recapture (TOCTOU check) — should match pre_text unless
    #      `recapture_text` overrides;
    #   2) post-paste baseline — should be post_text.
    recapture_value = recapture_text if recapture_text is not None else pre_text
    capture_returns = [recapture_value, post_text]
    capture_mock = AsyncMock(side_effect=capture_returns)
    monkeypatch.setattr(tools, "capture_doc_plain", capture_mock)

    async def fake_backup(editor, dirpath, doc_url_or_id=None):
        target = doc_url_or_id or page.url
        if backup_capture_failed:
            return {
                "txt": None,
                "html": None,
                "doc_url": page.url,
                "doc_id": drift_mod.extract_doc_id(target),
                "warning": None,
                "error": "clipboard.read failed: NotAllowedError",
            }
        base = drift_mod.backup_base_path(Path(dirpath), target)
        txt_path = base.with_suffix(".txt")
        drift_mod.atomic_write_text(txt_path, pre_text)
        html_path = base.with_suffix(".html")
        drift_mod.atomic_write_text(html_path, f"<div>{pre_text}</div>")
        return {
            "txt": txt_path,
            "html": html_path,
            "doc_url": page.url,
            "doc_id": drift_mod.extract_doc_id(target),
            "warning": None,
            "error": None,
        }

    monkeypatch.setattr(tools, "backup_doc", fake_backup)

    # v0.3.4: every docs_replace_all path now runs _verify_paste_landed before
    # saving baseline. Default the mock to "verified" so existing scenarios
    # don't have to thread fingerprint matching through their mocks; failure
    # paths override this in their own tests.
    async def fake_verify(editor, source_plain):
        return True, post_text, {
            "fingerprint": (source_plain or "")[:60],
            "fingerprint_present": True,
            "retries": 0,
            "source_chars": len(source_plain or ""),
            "captured_chars": len(post_text or ""),
        }

    monkeypatch.setattr(tools, "_verify_paste_landed", fake_verify)
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
    page, backup_dir = _patch_common(monkeypatch, tmp_path)
    # restore calls capture_doc_plain exactly once (post-restore baseline);
    # override the multi-shot mock from _patch_common with a fixed return.
    monkeypatch.setattr(
        tools, "capture_doc_plain", AsyncMock(return_value="restored")
    )
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


# -----------------------------------------------------------------------------
# v0.3.1 — codex audit regressions
# -----------------------------------------------------------------------------


async def test_replace_all_fails_closed_on_capture_failure(monkeypatch, tmp_path):
    # HIGH #2: pre-op clipboard read failure must abort destructive replace
    # unless the caller explicitly passes force=True.
    _patch_common(monkeypatch, tmp_path, backup_capture_failed=True)
    res = await tools.docs_replace_all("# x", "markdown")
    assert res["success"] is False
    assert res["capture_failed"] is True
    assert "force=True" in res["error"]
    tools.clear_doc.assert_not_awaited()  # type: ignore[attr-defined]
    tools.paste_html.assert_not_awaited()  # type: ignore[attr-defined]


async def test_replace_all_capture_failure_force_proceeds(monkeypatch, tmp_path):
    # force=True must override the capture-failure abort but surface the flag.
    page, _ = _patch_common(
        monkeypatch, tmp_path, backup_capture_failed=True, post_text="new"
    )
    res = await tools.docs_replace_all("# x", "markdown", force=True)
    assert res["success"] is True
    assert res["capture_failed"] is True
    assert res["forced"] is True


async def test_replace_all_detects_toctou_between_snapshot_and_clear(
    monkeypatch, tmp_path
):
    # HIGH #4: between backup_doc and clear_doc, the user landed an edit.
    # The pre-clear recapture diverges from the snapshot — abort.
    page, backup_dir = _patch_common(
        monkeypatch,
        tmp_path,
        pre_text="snapshot says A",
        recapture_text="user just edited to B",
    )
    res = await tools.docs_replace_all("# x", "markdown")
    assert res["success"] is False
    assert res["toctou_detected"] is True
    assert "toctou_summary" in res and res["toctou_summary"]
    tools.clear_doc.assert_not_awaited()  # type: ignore[attr-defined]


async def test_replace_all_toctou_force_proceeds(monkeypatch, tmp_path):
    _patch_common(
        monkeypatch,
        tmp_path,
        pre_text="snapshot A",
        recapture_text="diverged B",
        post_text="new content",
    )
    res = await tools.docs_replace_all("# x", "markdown", force=True)
    assert res["success"] is True
    assert res["toctou_detected"] is True
    assert res["forced"] is True


async def test_replace_all_post_clear_paste_failure_surfaces_recovery(
    monkeypatch, tmp_path
):
    # LOW #8: when paste_html fails after clear_doc has already wiped the
    # Doc, the response must shout "RESTORE FROM BACKUP" — not just return
    # a generic clipboard error.
    _patch_common(
        monkeypatch,
        tmp_path,
        paste_status="CLIP_ERR NotAllowedError: focus lost",
    )
    res = await tools.docs_replace_all("# x", "markdown")
    assert res["success"] is False
    assert res["doc_may_be_empty"] is True
    assert res["recommended_next_action"] == "docs_restore_from_backup"
    assert res["backup_paths"]["html"] is not None


async def test_check_drift_advertises_clipboard_side_effects(monkeypatch, tmp_path):
    # MEDIUM #6: docs_check_drift must declare it overwrites the clipboard
    # and changes the selection so callers can warn the user.
    # v0.3.2 also declares tab_focus_changed (verification audit residual).
    _patch_common(monkeypatch, tmp_path, post_text="current text")
    res = await tools.docs_check_drift()
    assert res["success"] is True
    assert res["clipboard_overwritten"] is True
    assert res["selection_changed"] is True
    assert res["tab_focus_changed"] is True


async def test_replace_all_uses_safe_doc_key_for_baseline(monkeypatch, tmp_path):
    # CRITICAL #1 regression: distinct doc ids sharing a 16-char prefix must
    # produce distinct baseline files. Pre-v0.3.1 they collided.
    pin_a = "https://docs.google.com/document/d/AAAAAAAAAAAAAAAA_first/edit"
    pin_b = "https://docs.google.com/document/d/AAAAAAAAAAAAAAAA_second/edit"

    page_a, backup_dir = _patch_common(monkeypatch, tmp_path, post_text="A content")
    page_a.url = pin_a
    await tools.docs_replace_all("# a", "markdown", doc_url=pin_a)

    page_b, _ = _patch_common(monkeypatch, tmp_path, post_text="B content")
    page_b.url = pin_b
    await tools.docs_replace_all("# b", "markdown", doc_url=pin_b)

    baseline_a = drift_mod.last_push_path(backup_dir, pin_a)
    baseline_b = drift_mod.last_push_path(backup_dir, pin_b)
    assert baseline_a != baseline_b
    assert baseline_a.read_text() == "A content"
    assert baseline_b.read_text() == "B content"


async def test_replace_all_hostile_doc_id_does_not_escape_backup_dir(
    monkeypatch, tmp_path
):
    # MEDIUM #5 regression: a crafted doc URL must not be able to write a
    # baseline outside the backup directory.
    page, backup_dir = _patch_common(monkeypatch, tmp_path, post_text="content")
    hostile = "https://docs.google.com/document/d/../../../../etc/passwd_pwn/edit"
    page.url = hostile
    res = await tools.docs_replace_all("# x", "markdown", doc_url=hostile)
    assert res["success"] is True
    baseline_path = Path(res["baseline_path"])
    # The path must stay inside backup_dir, regardless of the hostile input.
    assert baseline_dir_ancestor(backup_dir, baseline_path)
    # Filename should be the hashed fallback (`_last_pushed_h_<32hex>.txt`).
    assert baseline_path.name.startswith("_last_pushed_h_")


def baseline_dir_ancestor(parent: Path, child: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


# -----------------------------------------------------------------------------
# v0.3.4 — Bug 2 regressions: paste verification + baseline corruption guard.
# -----------------------------------------------------------------------------


def test_content_to_verification_plain_strips_html():
    out = tools._content_to_verification_plain(
        "<h1>Hi</h1>  <p>world  again</p>", "html"
    )
    assert "<" not in out and ">" not in out
    assert "  " not in out  # whitespace collapsed
    assert "Hi" in out and "world" in out


def test_content_to_verification_plain_preserves_markdown_text():
    out = tools._content_to_verification_plain(
        "## Heading\n\nliquidity fragmentation removed", "markdown"
    )
    assert "Heading" in out
    assert "liquidity fragmentation removed" in out


def test_pick_fingerprint_short_content_returned_verbatim():
    assert tools._pick_fingerprint("short") == "short"


def test_pick_fingerprint_long_content_returns_middle_chunk():
    long = "PREFIX " * 5 + "DISTINCTIVE_MIDDLE_CHUNK_KEYWORD " + "SUFFIX " * 5
    fp = tools._pick_fingerprint(long)
    assert "DISTINCTIVE_MIDDLE_CHUNK_KEYWORD" in fp or "PREFIX" in fp
    assert 0 < len(fp) <= 80


async def test_replace_all_aborts_when_paste_verification_fails(monkeypatch, tmp_path):
    # Real production incident: paste silently no-opped, capture saw old
    # content, baseline got saved as old content, next inject saw "drift"
    # (user's real edits vs corrupted baseline) and force=True overwrote them.
    # Defense: refuse to save baseline when verification fails.
    page, backup_dir = _patch_common(monkeypatch, tmp_path, post_text="paste-OK")

    async def fake_verify_fails(editor, source_plain):
        return False, "OLD CONTENT (paste no-opped)", {
            "fingerprint": (source_plain or "")[:60],
            "fingerprint_present": False,
            "retries": 1,
            "source_chars": len(source_plain or ""),
            "captured_chars": 30,
        }

    monkeypatch.setattr(tools, "_verify_paste_landed", fake_verify_fails)
    res = await tools.docs_replace_all("liquidity fragmentation removed", "markdown")
    assert res["success"] is False
    assert res["paste_verification_failed"] is True
    assert res["baseline_saved"] is False
    assert res["recommended_next_action"]  # non-empty actionable string
    # Baseline file must NOT exist after a verification failure.
    doc_id = drift_mod.safe_doc_key(page.url)
    assert not drift_mod.last_push_path(backup_dir, doc_id).exists()


async def test_replace_all_saves_baseline_only_when_verified(monkeypatch, tmp_path):
    page, backup_dir = _patch_common(monkeypatch, tmp_path, post_text="verified-text")
    res = await tools.docs_replace_all("# anything", "markdown")
    assert res["success"] is True
    assert res["paste_verified"] is True
    assert res["baseline_saved"] is True
    doc_id = drift_mod.safe_doc_key(page.url)
    assert drift_mod.last_push_path(backup_dir, doc_id).read_text() == "verified-text"


async def test_replace_all_surfaces_drift_hunk_meta(monkeypatch, tmp_path):
    # Bug 1 regression at the tools.py level: agent receives hunk counts so
    # it can't be tricked by a truncated diff into running force=True blind.
    page, backup_dir = _patch_common(
        monkeypatch, tmp_path, pre_text="baseline content"
    )
    doc_id = drift_mod.safe_doc_key(page.url)
    drift_mod.save_last_push(
        "different baseline\nwith multiple\nlines that differ", doc_id, backup_dir
    )
    res = await tools.docs_replace_all("# new", "markdown")
    assert res["success"] is False
    assert res["drift_detected"] is True
    assert "drift_hunks_total" in res
    assert isinstance(res["drift_hunks_total"], int)
    assert res["drift_hunks_total"] >= 1
    assert "drift_truncated" in res
