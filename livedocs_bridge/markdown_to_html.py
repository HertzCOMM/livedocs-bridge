"""Markdown → HTML conversion tuned for Google Docs paste rendering.

Docs renders pasted HTML as native formatted content (headings, tables, bold).
Pasting raw markdown produces literal `##` and `|` characters instead.
"""

from __future__ import annotations

import markdown as _markdown

_DEFAULT_EXTENSIONS = ["tables", "fenced_code", "nl2br"]


def md_to_html(text: str, extensions: list[str] | None = None) -> str:
    """Convert markdown text to HTML suitable for Google Docs clipboard paste.

    Args:
        text: Markdown source.
        extensions: Optional override of python-markdown extensions.
            Default: tables + fenced_code + nl2br.

    Returns:
        HTML string. Always wraps in a single <div> so the clipboard
        ClipboardItem has a stable root element.
    """
    exts = extensions if extensions is not None else _DEFAULT_EXTENSIONS
    body = _markdown.markdown(text, extensions=exts)
    return f"<div>{body}</div>"
