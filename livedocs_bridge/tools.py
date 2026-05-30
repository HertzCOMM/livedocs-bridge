"""Tool implementations exposed through the MCP server.

Every tool returns a plain dict (JSON-serializable). On failure the dict
shape is `{"success": False, "error": "<message>", ...}` so MCP clients
can surface the error without an exception bubbling up.
"""

from __future__ import annotations

import base64
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from .markdown_to_html import md_to_html
from .playwright_core import (
    BrowserSession,
    clear_doc,
    find_or_open_doc,
    get_doc_text,
    get_doc_title,
    get_docs_editor,
    insert_text,
    move_caret_to_end,
    paste_html,
    scroll_doc,
)

log = logging.getLogger("livedocs_bridge.tools")


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    return {"success": True, **data}


def _err(msg: str, **extra: Any) -> dict[str, Any]:
    return {"success": False, "error": msg, **extra}


def _resolve_content(content: str, content_type: str) -> tuple[str, str]:
    """Return (html, char_count_for_telemetry).

    `content_type` is 'markdown' (default) or 'html'.
    """
    ctype = (content_type or "markdown").strip().lower()
    if ctype == "html":
        return content, str(len(content))
    if ctype == "markdown":
        return md_to_html(content), str(len(content))
    raise ValueError(f"Unsupported content_type: {content_type!r}")


async def docs_open(url: str) -> dict[str, Any]:
    """Open Google Doc URL in attached browser. Auto-creates if it's docs.new."""
    target = url or "https://docs.new"
    try:
        async with BrowserSession() as session:
            await session.grant_clipboard()
            page = await find_or_open_doc(session, target)
            title = await get_doc_title(page)
            return _ok({"doc_url": page.url, "title": title, "tab_id": _tab_id(page)})
    except Exception as e:
        log.exception("docs_open failed")
        return _err(str(e), doc_url=target)


async def docs_replace_all(content: str, content_type: str = "markdown") -> dict[str, Any]:
    """Wholesale replace Doc content. content_type: 'markdown' | 'html'."""
    try:
        html, _ = _resolve_content(content, content_type)
    except ValueError as e:
        return _err(str(e))
    try:
        async with BrowserSession() as session:
            await session.grant_clipboard()
            page = await find_or_open_doc(session)
            editor = await get_docs_editor(page)
            await clear_doc(editor)
            status = await paste_html(editor, html)
            if not status.startswith("CLIP_OK"):
                return _err(f"clipboard write failed: {status}", doc_url=page.url)
            return _ok(
                {
                    "doc_url": page.url,
                    "chars_injected": len(content),
                    "html_bytes": len(html),
                }
            )
    except Exception as e:
        log.exception("docs_replace_all failed")
        return _err(str(e))


async def docs_append(content: str, content_type: str = "markdown") -> dict[str, Any]:
    """Append to end of Doc (Cmd+End + paste)."""
    try:
        html, _ = _resolve_content(content, content_type)
    except ValueError as e:
        return _err(str(e))
    try:
        async with BrowserSession() as session:
            await session.grant_clipboard()
            page = await find_or_open_doc(session)
            editor = await get_docs_editor(page)
            await move_caret_to_end(editor)
            status = await paste_html(editor, html)
            if not status.startswith("CLIP_OK"):
                # Fall back to raw insert_text — slower but no clipboard dependency.
                await move_caret_to_end(editor)
                await insert_text(editor, content)
            return _ok({"doc_url": page.url, "chars_appended": len(content)})
    except Exception as e:
        log.exception("docs_append failed")
        return _err(str(e))


async def docs_find_replace(
    find: str, replace: str, all_occurrences: bool = True
) -> dict[str, Any]:
    """Replace occurrences of `find` with `replace` by re-typing the affected runs.

    Implementation note: Google Docs renders body content to canvas and exposes
    no public DOM mutation API. We use the in-app find/replace dialog
    (Cmd+Shift+H) via keyboard. The dialog is rendered in the top frame so
    page-scoped keyboard works for it.
    """
    if not find:
        return _err("`find` must be non-empty")
    try:
        async with BrowserSession() as session:
            page = await find_or_open_doc(session)
            # Make sure focus is somewhere in the doc body first.
            editor = await get_docs_editor(page)
            await editor.editable.focus()
            await page.keyboard.press("Meta+Shift+H")
            await page.wait_for_timeout(800)
            replaced = await _drive_find_replace_dialog(
                page, find, replace, all_occurrences
            )
            return _ok({"doc_url": page.url, "replaced_count": replaced})
    except Exception as e:
        log.exception("docs_find_replace failed")
        return _err(str(e))


async def _drive_find_replace_dialog(
    page, find: str, replace: str, all_occurrences: bool
) -> int:
    """Type into the Docs Find & Replace dialog and click Replace / Replace all.

    Returns a best-effort replacement count. Docs reports counts in the dialog
    status line; we read it back via DOM if available, else return -1.
    """
    # Locate find input (placeholder localized; we use aria-label heuristics).
    find_input = page.locator(
        'input[aria-label*="Find" i], input[aria-label*="查找" i]'
    ).first
    await find_input.wait_for(timeout=5000)
    await find_input.fill(find)
    replace_input = page.locator(
        'input[aria-label*="Replace with" i], input[aria-label*="替换" i]'
    ).first
    await replace_input.fill(replace)

    if all_occurrences:
        btn = page.locator(
            'button:has-text("Replace all"), button:has-text("全部替换")'
        ).first
    else:
        btn = page.locator(
            'button:has-text("Replace"):not(:has-text("all")):not(:has-text("全部")),'
            ' button:has-text("替换"):not(:has-text("全部"))'
        ).first
    await btn.click()
    await page.wait_for_timeout(800)

    count = -1
    try:
        msg = await page.evaluate(
            """
            () => {
              const sel = '[role="dialog"] [aria-live], [role="dialog"] .docs-replacedialog-message';
              const el = document.querySelector(sel);
              return el ? el.textContent : null;
            }
            """
        )
        if isinstance(msg, str):
            digits = "".join(ch for ch in msg if ch.isdigit())
            if digits:
                count = int(digits)
    except Exception:
        pass

    # Close dialog so subsequent ops can target the body.
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(200)
    return count


async def docs_screenshot(
    scroll_to: str = "top", path: Optional[str] = None
) -> dict[str, Any]:
    """Capture the current Docs viewport. scroll_to: 'top' | 'bottom' | 'current'."""
    where = (scroll_to or "current").strip().lower()
    if where not in {"top", "bottom", "current"}:
        return _err(f"invalid scroll_to: {scroll_to!r}")
    try:
        async with BrowserSession() as session:
            page = await find_or_open_doc(session)
            await scroll_doc(page, where)
            if path:
                out_path = Path(path).expanduser().resolve()
                out_path.parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(out_path), full_page=False)
                return _ok({"png_path": str(out_path), "doc_url": page.url})
            fd, tmp = tempfile.mkstemp(prefix="livedocs_", suffix=".png")
            os.close(fd)
            await page.screenshot(path=tmp, full_page=False)
            data = Path(tmp).read_bytes()
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return _ok(
                {
                    "png_base64": base64.b64encode(data).decode("ascii"),
                    "doc_url": page.url,
                    "byte_count": len(data),
                }
            )
    except Exception as e:
        log.exception("docs_screenshot failed")
        return _err(str(e))


async def docs_get_state() -> dict[str, Any]:
    """Return Doc URL, title, char count, current timestamp."""
    try:
        async with BrowserSession() as session:
            page = await find_or_open_doc(session)
            title = await get_doc_title(page)
            text = await get_doc_text(page)
            return _ok(
                {
                    "doc_url": page.url,
                    "title": title,
                    "char_count": len(text or ""),
                    "observed_at_unix": int(time.time()),
                }
            )
    except Exception as e:
        log.exception("docs_get_state failed")
        return _err(str(e))


def _tab_id(page) -> str:
    """Best-effort stable identifier for a tab. CDP `targetId` would be ideal
    but is not exposed on Playwright Page; we hash url+title as a proxy."""
    try:
        return str(abs(hash((page.url, page.url.split("/")[-1]))))
    except Exception:
        return "unknown"
