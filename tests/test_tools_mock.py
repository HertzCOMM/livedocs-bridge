"""Mock-based smoke tests for tool functions.

These tests patch `BrowserSession` and the helper functions so we exercise
the tool wrappers' control flow (success path, error path, content_type
validation) without needing a live Chrome.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from livedocs_bridge import tools


class _FakePage:
    def __init__(self, url="https://docs.google.com/document/d/FAKE/edit"):
        self.url = url


@asynccontextmanager
async def _fake_session():
    s = MagicMock()
    s.grant_clipboard = AsyncMock()
    yield s


def _patch_common(monkeypatch, *, paste_status="CLIP_OK", title="Untitled"):
    page = _FakePage()
    monkeypatch.setattr(
        tools, "BrowserSession", lambda *a, **k: _fake_session()
    )
    monkeypatch.setattr(tools, "find_or_open_doc", AsyncMock(return_value=page))
    monkeypatch.setattr(tools, "get_docs_editor", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(tools, "clear_doc", AsyncMock())
    monkeypatch.setattr(tools, "move_caret_to_end", AsyncMock())
    monkeypatch.setattr(tools, "paste_html", AsyncMock(return_value=paste_status))
    monkeypatch.setattr(tools, "insert_text", AsyncMock())
    monkeypatch.setattr(tools, "get_doc_title", AsyncMock(return_value=title))
    monkeypatch.setattr(
        tools, "get_doc_text", AsyncMock(return_value="hello world")
    )
    monkeypatch.setattr(tools, "scroll_doc", AsyncMock())
    return page


async def test_docs_open_success(monkeypatch):
    _patch_common(monkeypatch, title="My Memo")
    res = await tools.docs_open("https://docs.google.com/document/d/FAKE/edit")
    assert res["success"] is True
    assert res["title"] == "My Memo"
    assert "doc_url" in res
    assert "tab_id" in res


async def test_docs_replace_all_markdown(monkeypatch):
    _patch_common(monkeypatch)
    res = await tools.docs_replace_all("# Hi\n\nbody", "markdown")
    assert res["success"] is True
    assert res["chars_injected"] == len("# Hi\n\nbody")
    assert res["html_bytes"] > 0


async def test_docs_replace_all_html(monkeypatch):
    _patch_common(monkeypatch)
    res = await tools.docs_replace_all("<p>hi</p>", "html")
    assert res["success"] is True
    assert res["chars_injected"] == len("<p>hi</p>")


async def test_docs_replace_all_rejects_unknown_content_type(monkeypatch):
    _patch_common(monkeypatch)
    res = await tools.docs_replace_all("hi", "rtf")
    assert res["success"] is False
    assert "content_type" in res["error"].lower()


async def test_docs_replace_all_propagates_clipboard_failure(monkeypatch):
    _patch_common(monkeypatch, paste_status="CLIP_ERR NotAllowedError: focus lost")
    res = await tools.docs_replace_all("# hi", "markdown")
    assert res["success"] is False
    assert "CLIP_ERR" in res["error"]


async def test_docs_append_falls_back_to_insert_text(monkeypatch):
    _patch_common(monkeypatch, paste_status="CLIP_ERR")
    res = await tools.docs_append("appended", "markdown")
    assert res["success"] is True
    assert res["chars_appended"] == len("appended")
    tools.insert_text.assert_awaited()  # type: ignore[attr-defined]


async def test_docs_find_replace_empty_find_rejected(monkeypatch):
    res = await tools.docs_find_replace("", "anything", True)
    assert res["success"] is False
    assert "non-empty" in res["error"]


async def test_docs_get_state_returns_char_count(monkeypatch):
    _patch_common(monkeypatch, title="State Doc")
    res = await tools.docs_get_state()
    assert res["success"] is True
    assert res["title"] == "State Doc"
    assert res["char_count"] == len("hello world")
    assert isinstance(res["observed_at_unix"], int)


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
