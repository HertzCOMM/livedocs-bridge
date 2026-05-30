"""Tool implementations exposed through the MCP server.

Every tool returns a plain dict (JSON-serializable). On failure the dict
shape is `{"success": False, "error": "<message>", ...}` so MCP clients
can surface the error without an exception bubbling up.

v0.3.0 hardening (source: HertzFlow × WLFI memo session):
- `docs_replace_all` / `docs_append` snapshot the Doc to a persistent backup
  dir + check for drift against the last push before clearing. Drift aborts
  unless the caller passes `force=True`.
- `doc_url` (recommended) pins the target Doc by id so a stale tab can't
  redirect the inject to the wrong place.
- Two new tools: `docs_check_drift` (preview) and `docs_restore_from_backup`
  (recover from the most recent snapshot).
"""

from __future__ import annotations

import base64
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from . import drift as drift_mod
from .markdown_to_html import md_to_html
from .playwright_core import (
    BrowserSession,
    backup_doc,
    capture_doc_plain,
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


def _paths_dict(backup: dict[str, Any]) -> dict[str, Optional[str]]:
    return {
        "txt": str(backup["txt"]) if backup.get("txt") else None,
        "html": str(backup["html"]) if backup.get("html") else None,
    }


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


async def docs_replace_all(
    content: str,
    content_type: str = "markdown",
    doc_url: Optional[str] = None,
    force: bool = False,
) -> dict[str, Any]:
    """Wholesale-replace Doc content with drift protection.

    Args:
        content: markdown or html payload.
        content_type: 'markdown' (default) or 'html'.
        doc_url: pin to this Doc id. RECOMMENDED — without it we fall back to
            the first matching tab, which can be the wrong Doc.
        force: bypass drift abort. The Doc is still backed up before clearing.
    """
    try:
        html, _ = _resolve_content(content, content_type)
    except ValueError as e:
        return _err(str(e))

    backup_dir = drift_mod.default_backup_dir()
    backup_dir.mkdir(parents=True, exist_ok=True)
    drift_mod.prune_old_backups(backup_dir)

    try:
        async with BrowserSession() as session:
            await session.grant_clipboard()
            page = await find_or_open_doc(session, doc_url)
            editor = await get_docs_editor(page)

            backup = await backup_doc(editor, backup_dir, doc_url_or_id=doc_url or page.url)
            doc_id = backup.get("doc_id") or drift_mod.extract_doc_id(page.url) or "unknown"
            current_plain = ""
            if backup.get("txt"):
                try:
                    current_plain = backup["txt"].read_text(encoding="utf-8")
                except OSError:
                    current_plain = ""

            drifted, diff = drift_mod.check_drift(current_plain, doc_id, backup_dir)
            if drifted and not force:
                return _err(
                    "Doc drifted since last push; refusing to overwrite without force=True.",
                    doc_url=page.url,
                    drift_detected=True,
                    drift_summary=diff,
                    backup_paths=_paths_dict(backup),
                )

            await clear_doc(editor)
            status = await paste_html(editor, html)
            if not status.startswith("CLIP_OK"):
                return _err(
                    f"clipboard write failed: {status}",
                    doc_url=page.url,
                    backup_paths=_paths_dict(backup),
                )

            new_plain = await capture_doc_plain(editor)
            baseline_path = drift_mod.save_last_push(new_plain, doc_id, backup_dir)
            return _ok(
                {
                    "doc_url": page.url,
                    "chars_injected": len(content),
                    "html_bytes": len(html),
                    "drift_detected": drifted,
                    "drift_summary": diff if drifted else "",
                    "backup_paths": _paths_dict(backup),
                    "baseline_saved": True,
                    "baseline_path": str(baseline_path),
                    "forced": bool(drifted and force),
                }
            )
    except Exception as e:
        log.exception("docs_replace_all failed")
        return _err(str(e))


async def docs_append(
    content: str,
    content_type: str = "markdown",
    doc_url: Optional[str] = None,
) -> dict[str, Any]:
    """Append to end of Doc. Non-destructive — no drift check, but does snapshot."""
    try:
        html, _ = _resolve_content(content, content_type)
    except ValueError as e:
        return _err(str(e))
    backup_dir = drift_mod.default_backup_dir()
    backup_dir.mkdir(parents=True, exist_ok=True)
    try:
        async with BrowserSession() as session:
            await session.grant_clipboard()
            page = await find_or_open_doc(session, doc_url)
            editor = await get_docs_editor(page)
            backup = await backup_doc(editor, backup_dir, doc_url_or_id=doc_url or page.url)
            await move_caret_to_end(editor)
            status = await paste_html(editor, html)
            if not status.startswith("CLIP_OK"):
                # Fall back to raw insert_text — slower but no clipboard dependency.
                await move_caret_to_end(editor)
                await insert_text(editor, content)
            return _ok(
                {
                    "doc_url": page.url,
                    "chars_appended": len(content),
                    "backup_paths": _paths_dict(backup),
                }
            )
    except Exception as e:
        log.exception("docs_append failed")
        return _err(str(e))


async def docs_find_replace(
    find: str, replace: str, all_occurrences: bool = True
) -> dict[str, Any]:
    """Replace occurrences of `find` with `replace` via the Docs Find & Replace dialog."""
    if not find:
        return _err("`find` must be non-empty")
    try:
        async with BrowserSession() as session:
            page = await find_or_open_doc(session)
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

    await page.keyboard.press("Escape")
    await page.wait_for_timeout(200)
    return count


async def docs_screenshot(
    scroll_to: str = "top", path: Optional[str] = None
) -> dict[str, Any]:
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


async def docs_check_drift(doc_url: Optional[str] = None) -> dict[str, Any]:
    """Preview whether the Doc has changed since our last push.

    Use this before `docs_replace_all` if you want to surface the diff to the
    user instead of blindly calling replace and getting an error.
    """
    backup_dir = drift_mod.default_backup_dir()
    try:
        async with BrowserSession() as session:
            await session.grant_clipboard()
            page = await find_or_open_doc(session, doc_url)
            editor = await get_docs_editor(page)
            current_plain = await capture_doc_plain(editor)
            doc_id = (
                drift_mod.extract_doc_id(doc_url) if doc_url else None
            ) or drift_mod.extract_doc_id(page.url) or "unknown"
            baseline_path = drift_mod.last_push_path(backup_dir, doc_id)
            drifted, diff = drift_mod.check_drift(current_plain, doc_id, backup_dir)
            return _ok(
                {
                    "doc_url": page.url,
                    "doc_id": doc_id,
                    "drifted": drifted,
                    "drift_summary": diff,
                    "baseline_exists": baseline_path.exists(),
                    "baseline_path": str(baseline_path) if baseline_path.exists() else None,
                }
            )
    except Exception as e:
        log.exception("docs_check_drift failed")
        return _err(str(e))


async def docs_restore_from_backup(
    doc_url: Optional[str] = None,
    backup_timestamp: Optional[str] = None,
) -> dict[str, Any]:
    """Replace the Doc with a previously saved HTML backup.

    Latest backup is used when `backup_timestamp` is omitted. Bypasses drift
    abort (restoring from backup is the recovery path itself), but still
    snapshots the pre-restore state first so an erroneous restore can be undone.
    """
    backup_dir = drift_mod.default_backup_dir()
    pin = doc_url
    try:
        async with BrowserSession() as session:
            await session.grant_clipboard()
            page = await find_or_open_doc(session, pin)
            doc_id_lookup = pin or page.url
            entry = drift_mod.find_backup(doc_id_lookup, backup_timestamp, backup_dir)
            if entry is None:
                return _err(
                    "no backup found for this Doc",
                    doc_url=page.url,
                    backup_dir=str(backup_dir),
                )
            html_path: Optional[Path] = entry.get("html")
            txt_path: Optional[Path] = entry.get("txt")
            if html_path is None and txt_path is None:
                return _err(
                    "matching backup has no content files",
                    doc_url=page.url,
                    timestamp=entry["timestamp"],
                )

            editor = await get_docs_editor(page)
            # Snapshot current state before overwriting it with the restore.
            pre_restore = await backup_doc(
                editor, backup_dir, doc_url_or_id=pin or page.url
            )

            await clear_doc(editor)
            if html_path is not None:
                html = html_path.read_text(encoding="utf-8")
                status = await paste_html(editor, html)
                if not status.startswith("CLIP_OK"):
                    return _err(
                        f"clipboard write failed during restore: {status}",
                        doc_url=page.url,
                        pre_restore_backup=_paths_dict(pre_restore),
                    )
            else:
                # No HTML backup — fall back to plain text via insert_text.
                await insert_text(editor, txt_path.read_text(encoding="utf-8"))

            new_plain = await capture_doc_plain(editor)
            doc_id = (
                drift_mod.extract_doc_id(doc_id_lookup) or "unknown"
            )
            baseline_path = drift_mod.save_last_push(new_plain, doc_id, backup_dir)
            return _ok(
                {
                    "doc_url": page.url,
                    "restored_from": str(html_path or txt_path),
                    "restored_timestamp": entry["timestamp"],
                    "pre_restore_backup": _paths_dict(pre_restore),
                    "baseline_path": str(baseline_path),
                }
            )
    except Exception as e:
        log.exception("docs_restore_from_backup failed")
        return _err(str(e))


def _tab_id(page) -> str:
    """Best-effort stable identifier for a tab."""
    try:
        return str(abs(hash((page.url, page.url.split("/")[-1]))))
    except Exception:
        return "unknown"
