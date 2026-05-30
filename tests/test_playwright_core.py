"""Unit tests for playwright_core helpers that don't require a live browser."""

from __future__ import annotations

import os

import pytest

from livedocs_bridge import playwright_core as core
from livedocs_bridge.markdown_to_html import md_to_html


def test_default_cdp_url_constant():
    assert core.DEFAULT_CDP_URL == "http://127.0.0.1:19825"


def test_get_cdp_url_env_override(monkeypatch):
    monkeypatch.setenv("LIVEDOCS_CDP_URL", "http://192.168.1.10:9222")
    assert core.get_cdp_url() == "http://192.168.1.10:9222"


def test_get_cdp_url_default(monkeypatch):
    monkeypatch.delenv("LIVEDOCS_CDP_URL", raising=False)
    assert core.get_cdp_url() == core.DEFAULT_CDP_URL


def test_doc_match_key_extracts_id():
    url = "https://docs.google.com/document/d/ABC123_xyz/edit?usp=sharing"
    assert core._doc_match_key(url) == "/document/d/ABC123_xyz"


def test_doc_match_key_docs_new_returns_none():
    assert core._doc_match_key("https://docs.new") is None


def test_doc_match_key_no_id_falls_back_to_fragment():
    assert (
        core._doc_match_key("https://docs.google.com/document/u/0/")
        == "docs.google.com/document"
    )


def test_doc_match_key_garbage_url_returns_none():
    assert core._doc_match_key("https://example.com") is None


def test_md_to_html_renders_heading():
    html = md_to_html("# Hello")
    assert "<h1>Hello</h1>" in html
    assert html.startswith("<div>") and html.endswith("</div>")


def test_md_to_html_renders_table():
    md = "| a | b |\n| --- | --- |\n| 1 | 2 |\n"
    html = md_to_html(md)
    assert "<table>" in html
    assert "<td>1</td>" in html


def test_md_to_html_empty_string():
    assert md_to_html("") == "<div></div>"
