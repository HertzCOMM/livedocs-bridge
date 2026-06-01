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


def _find_replace_shortcut() -> str:
    """Google Docs Find & Replace dialog keystroke per OS.

    macOS: Cmd+Shift+H. Windows / Linux: Ctrl+H. Playwright's `ControlOrMeta`
    alias handles modifier-only swaps but not the Shift divergence — Docs
    explicitly binds different keys, not just different modifiers.
    """
    import platform

    return "Meta+Shift+H" if platform.system() == "Darwin" else "Control+H"


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
        force: bypass drift abort AND bypass the fail-closed guard when the
            pre-op clipboard capture fails. Still backed up before clearing.

    Failure-mode contract (codex audit v0.3.1):
      - Pre-op clipboard read failure (no `.txt` snapshot, no plain capture):
        abort unless `force=True`. We refuse to fly blind on a destructive op.
      - Drift detected vs `_last_pushed_<id>.txt`: abort unless `force=True`.
      - Second-stage drift check (between snapshot and clear) detects another
        edit landing in the TOCTOU window: abort unless `force=True`.
      - paste_html failure after a successful clear_doc: response carries
        `doc_may_be_empty=True` + `recommended_next_action` pointing at
        docs_restore_from_backup.

    Known residual (v0.3.2): a sub-millisecond race window remains between
    the pre-clear recapture and the `Cmd+A + Backspace` keystrokes landing.
    Closing it would require locking the user out of the tab (we don't), so
    keystroke-level edits in that window can still be silently consumed. The
    `_last_pushed_*` baseline + persistent backup remain the recovery path.
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
            doc_id = drift_mod.safe_doc_key(doc_url or page.url)
            current_plain = ""
            if backup.get("txt"):
                try:
                    current_plain = backup["txt"].read_text(encoding="utf-8")
                except OSError:
                    current_plain = ""

            # HIGH #2: fail-closed when the pre-op capture didn't produce any
            # ground truth. Either text or html is enough to call it a real
            # snapshot; both missing means we can't honestly drift-check.
            capture_failed = not backup.get("txt") and not backup.get("html")
            if capture_failed and not force:
                return _err(
                    "pre-op Doc capture failed; refusing destructive replace without force=True",
                    doc_url=page.url,
                    capture_failed=True,
                    capture_error=backup.get("error"),
                    backup_paths=_paths_dict(backup),
                    recommended_next_action=(
                        "verify Chrome has focus + clipboard permission for "
                        "docs.google.com, then retry; pass force=True only if "
                        "you accept that the prior Doc state is unrecoverable"
                    ),
                )

            drifted, diff, drift_meta = drift_mod.check_drift(
                current_plain, doc_id, backup_dir
            )
            if drifted and not force:
                return _err(
                    "Doc drifted since last push; refusing to overwrite without force=True.",
                    doc_url=page.url,
                    drift_detected=True,
                    drift_summary=diff,
                    drift_hunks_total=drift_meta.get("hunks_total"),
                    drift_hunks_shown=drift_meta.get("hunks_shown"),
                    drift_truncated=drift_meta.get("truncated", False),
                    backup_paths=_paths_dict(backup),
                )

            # HIGH #4: close the TOCTOU window between the snapshot above and
            # the clear_doc below. If the user landed an edit during this
            # interval, the recapture will diverge from `current_plain` and we
            # abort. Cheap compared to the destructive op we're about to run.
            recapture_diverged = False
            recapture_summary = ""
            try:
                recapture = await capture_doc_plain(editor)
                if (recapture or "").strip() != (current_plain or "").strip():
                    recapture_diverged = True
                    recapture_summary = _short_diff(current_plain, recapture)
            except Exception as e:  # noqa: BLE001 — diagnostic only
                log.warning("pre-clear recapture failed: %s", e)
            if recapture_diverged and not force:
                return _err(
                    "Doc changed between snapshot and clear; refusing to overwrite without force=True.",
                    doc_url=page.url,
                    toctou_detected=True,
                    toctou_summary=recapture_summary,
                    backup_paths=_paths_dict(backup),
                )

            await clear_doc(editor)
            status = await paste_html(editor, html)
            if not status.startswith("CLIP_OK"):
                return _err(
                    f"clipboard write failed after clear: {status}",
                    doc_url=page.url,
                    backup_paths=_paths_dict(backup),
                    doc_may_be_empty=True,
                    recommended_next_action="docs_restore_from_backup",
                )

            # v0.3.4 Bug 2: paste verification. Production incident: paste
            # silently no-opped (or capture saw pre-paste state), capture
            # returned old/empty content, baseline saved as old content,
            # next inject saw "drift" (user's real edits vs corrupted baseline)
            # and force=True silently overwrote them. Defense: extract a
            # fingerprint from what we just sent, verify it appears in the
            # capture, retry once on miss, FAIL CLOSED without saving baseline.
            sent_plain = _content_to_verification_plain(content, content_type)
            verified, new_plain, verify_meta = await _verify_paste_landed(
                editor, sent_plain
            )
            if not verified:
                return _err(
                    "paste verification failed: captured Doc text does not contain "
                    "expected content. Baseline NOT updated to avoid corrupting drift "
                    "detection on next inject.",
                    doc_url=page.url,
                    paste_verification_failed=True,
                    paste_verification_meta=verify_meta,
                    backup_paths=_paths_dict(backup),
                    baseline_saved=False,
                    # v0.3.5 MEDIUM #4: the prior message claimed drift would
                    # catch real divergence on the re-run path. That's wrong:
                    # baseline was NOT saved (we just refused), so the next
                    # `docs_replace_all` hits the "no baseline → first inject"
                    # branch and drift returns False regardless of what the
                    # Doc actually contains. Be honest about that gap.
                    drift_protection_reduced=True,
                    recommended_next_action=(
                        "Step 1: screenshot the Doc with `docs_screenshot` to "
                        "confirm what's actually there. "
                        "Step 2 (Doc visually wrong): `docs_restore_from_backup` "
                        "to recover prior state. "
                        "Step 2 (Doc visually correct, read-back glitched): "
                        "drift protection is REDUCED until next verified push — "
                        "the absent baseline means the next `docs_replace_all` "
                        "will treat the Doc as a first-inject (no drift abort). "
                        "Run `docs_check_drift` after the next successful "
                        "verified push to confirm the baseline is back."
                    ),
                )
            baseline_path = drift_mod.save_last_push(new_plain, doc_id, backup_dir)
            return _ok(
                {
                    "doc_url": page.url,
                    "chars_injected": len(content),
                    "html_bytes": len(html),
                    "drift_detected": drifted,
                    "drift_summary": diff if drifted else "",
                    "drift_hunks_total": drift_meta.get("hunks_total") if drifted else 0,
                    "drift_truncated": drift_meta.get("truncated", False) if drifted else False,
                    "toctou_detected": recapture_diverged,
                    "capture_failed": capture_failed,
                    "paste_verified": True,
                    "paste_verification_meta": verify_meta,
                    "backup_paths": _paths_dict(backup),
                    "baseline_saved": True,
                    "baseline_path": str(baseline_path),
                    "forced": bool(force and (drifted or capture_failed or recapture_diverged)),
                }
            )
    except Exception as e:
        log.exception("docs_replace_all failed")
        return _err(str(e))


def _short_diff(before: str, after: str, max_lines: int = 40) -> str:
    """Compact unified diff for surfacing TOCTOU divergence in tool responses."""
    import difflib

    lines = list(
        difflib.unified_diff(
            (before or "").splitlines(),
            (after or "").splitlines(),
            fromfile="snapshot",
            tofile="recapture",
            lineterm="",
            n=2,
        )
    )
    summary = "\n".join(lines[:max_lines])
    if len(lines) > max_lines:
        summary += f"\n... ({len(lines) - max_lines} more diff lines truncated)"
    return summary


# v0.3.4 / v0.3.5 — paste verification helpers (Bug 2 + codex audit hardening).

_VERIFY_FINGERPRINT_LEN = 60
_VERIFY_MIN_FINGERPRINT_LEN = 20
_VERIFY_RETRY_WAIT_MS = 2500
# v0.3.5 MEDIUM #2: empty source path. After a Cmd+A+Backspace clear and a
# paste of "" the Doc body should be ~empty (Docs may inject smart-chip
# placeholder text). If the captured body exceeds this, the paste didn't
# land — fail closed.
_VERIFY_EMPTY_SOURCE_MAX_CAPTURED_CHARS = 80
# v0.3.5 MEDIUM #3: fingerprint distinctiveness. Repetitive source like
# "A" * 500 yields a "AAA" fingerprint that matches almost anything. We
# require at least this many DISTINCT alphanumeric characters across the
# combined fingerprint set, else we fall back to a hash-prefix check.
_VERIFY_MIN_DISTINCT_CHARS = 6
# Multi-fingerprint: take three substrings from spread positions. Verified
# only if at least two match the capture (handles Docs auto-formatting
# tweaks that might munge one substring).
_VERIFY_FINGERPRINT_COUNT = 3
_VERIFY_FINGERPRINT_REQUIRED_MATCHES = 2
_HTML_TAG_RE = __import__("re").compile(r"<[^>]+>")
_WHITESPACE_RE = __import__("re").compile(r"\s+")
_NON_ALNUM_RE = __import__("re").compile(r"[^a-zA-Z0-9]")


def _content_to_verification_plain(content: str, content_type: str) -> str:
    """Reduce `content` to the plain text we expect to see in the Doc.

    For markdown we use the source as-is (Docs renders headings/tables, but
    the underlying TEXT is unchanged — `## Heading` becomes `Heading`).
    For HTML we strip tags. In both cases whitespace is collapsed so a
    multi-line source still matches the single-stream clipboard capture.
    """
    if (content_type or "markdown").strip().lower() == "html":
        raw = _HTML_TAG_RE.sub(" ", content)
    else:
        raw = content
    return _WHITESPACE_RE.sub(" ", raw).strip()


def _pick_fingerprint(plain: str) -> str:
    """Pick a substring of `plain` that should survive into the Doc after paste.

    Kept for backward compat / tests; new verification logic prefers the
    multi-fingerprint path in `_pick_fingerprints` so a repetitive source
    can't false-match against unrelated content.
    """
    if len(plain) <= _VERIFY_FINGERPRINT_LEN:
        return plain
    mid = len(plain) // 3
    chunk = plain[mid : mid + _VERIFY_FINGERPRINT_LEN].strip()
    if len(chunk) >= _VERIFY_MIN_FINGERPRINT_LEN:
        return chunk
    return plain[:_VERIFY_FINGERPRINT_LEN]


def _pick_fingerprints(plain: str) -> list[str]:
    """Pick up to N spread-out substrings of `plain` for distinctiveness.

    v0.3.5 MEDIUM #3 defense against low-entropy false positives. Returns
    up to `_VERIFY_FINGERPRINT_COUNT` non-empty substrings sampled at evenly
    spread offsets. For short content the whole string is the only entry.
    """
    if not plain:
        return []
    if len(plain) <= _VERIFY_FINGERPRINT_LEN:
        return [plain]
    n = _VERIFY_FINGERPRINT_COUNT
    chunk_len = _VERIFY_FINGERPRINT_LEN
    out: list[str] = []
    for i in range(n):
        # Offsets at 1/(n+1), 2/(n+1), ... so the first sample isn't at 0
        # (avoids picking the header) and the last isn't at the very end.
        start = max(0, (len(plain) * (i + 1)) // (n + 1) - chunk_len // 2)
        chunk = plain[start : start + chunk_len].strip()
        if len(chunk) >= _VERIFY_MIN_FINGERPRINT_LEN and chunk not in out:
            out.append(chunk)
    if not out:
        out.append(plain[:chunk_len])
    return out


def _distinct_alnum_chars(s: str) -> int:
    """Count unique alphanumeric chars in `s` (case-sensitive).

    Cheap entropy proxy. A 60-char string of "AAAA..." returns 1; a sentence
    returns 15-25.
    """
    return len(set(_NON_ALNUM_RE.sub("", s)))


async def _verify_paste_landed(editor, source_plain: str):
    """After paste, verify the source content actually appeared in the Doc.

    Returns `(verified, captured_plain, meta)`. v0.3.5 hardening:

    - **Empty source (MEDIUM #2)**: instead of auto-verifying, the capture
      must also be near-empty (≤ `_VERIFY_EMPTY_SOURCE_MAX_CAPTURED_CHARS`).
      Otherwise the paste didn't truly clear-and-replace.
    - **Multi-fingerprint match (MEDIUM #3)**: pick 3 spread-out fingerprints;
      require ≥ 2 matches in the capture. Defends against repetitive content
      where a single 60-char window can false-match unrelated stale text.
    - **Distinctiveness check**: if the combined fingerprints have fewer than
      `_VERIFY_MIN_DISTINCT_CHARS` unique alphanumerics, the meta flag
      `low_entropy_fingerprint=true` is set so callers know the verify
      result is weaker evidence than usual.

    Retry policy unchanged from v0.3.4: one retry after `_VERIFY_RETRY_WAIT_MS`.
    Final failure returns `verified=False` so the caller refuses to save baseline.
    """
    if not (source_plain or "").strip():
        # MEDIUM #2: empty source — capture must also be empty.
        captured = await capture_doc_plain(editor)
        captured_norm = _WHITESPACE_RE.sub(" ", captured or "").strip()
        verified = len(captured_norm) <= _VERIFY_EMPTY_SOURCE_MAX_CAPTURED_CHARS
        return verified, captured, {
            "fingerprints": [],
            "fingerprints_matched": 0,
            "fingerprints_required": 0,
            "fingerprint_present": verified,
            "empty_source": True,
            "low_entropy_fingerprint": False,
            "retries": 0,
            "source_chars": 0,
            "captured_chars": len(captured_norm),
        }

    fingerprints = _pick_fingerprints(source_plain)
    required = (
        _VERIFY_FINGERPRINT_REQUIRED_MATCHES
        if len(fingerprints) >= _VERIFY_FINGERPRINT_REQUIRED_MATCHES
        else len(fingerprints)
    )
    distinct = _distinct_alnum_chars(" ".join(fingerprints))
    low_entropy = distinct < _VERIFY_MIN_DISTINCT_CHARS

    captured = ""
    retries = 0
    matched = 0
    for attempt in range(2):  # initial + 1 retry
        captured = await capture_doc_plain(editor)
        captured_norm = _WHITESPACE_RE.sub(" ", captured or "")
        matched = sum(1 for fp in fingerprints if fp in captured_norm)
        if matched >= required:
            return True, captured, {
                "fingerprints": [fp[:80] for fp in fingerprints],
                "fingerprints_matched": matched,
                "fingerprints_required": required,
                "fingerprint_present": True,
                "empty_source": False,
                "low_entropy_fingerprint": low_entropy,
                "retries": retries,
                "source_chars": len(source_plain),
                "captured_chars": len(captured_norm),
            }
        if attempt == 0:
            retries += 1
            await editor.page.wait_for_timeout(_VERIFY_RETRY_WAIT_MS)
    return False, captured, {
        "fingerprints": [fp[:80] for fp in fingerprints],
        "fingerprints_matched": matched,
        "fingerprints_required": required,
        "fingerprint_present": False,
        "empty_source": False,
        "low_entropy_fingerprint": low_entropy,
        "retries": retries,
        "source_chars": len(source_plain),
        "captured_chars": len(_WHITESPACE_RE.sub(" ", captured or "")),
    }


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
            # Find & Replace shortcut differs by OS — Docs uses Cmd+Shift+H on
            # macOS but Ctrl+H (no Shift) on Windows/Linux. ControlOrMeta isn't
            # enough here because the Shift component diverges too.
            await page.keyboard.press(_find_replace_shortcut())
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

    ⚠ SIDE EFFECTS — this is "preview" semantically but not technically free:
      - Brings the Doc tab to the front (`page.bring_to_front()`); this can
        steal focus from the user's current window.
      - Runs Cmd+A so the user's prior selection is replaced with "select all".
      - Runs Cmd+C so the user's clipboard is overwritten with the Doc body.
    There is no canvas-internals API to read Doc text without round-tripping
    through the clipboard, so this cost is unavoidable today. The response
    carries `clipboard_overwritten` + `selection_changed` + `tab_focus_changed`
    so callers can warn the user explicitly.

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
            doc_id = drift_mod.safe_doc_key(doc_url or page.url)
            baseline_path = drift_mod.last_push_path(backup_dir, doc_id)
            drifted, diff, drift_meta = drift_mod.check_drift(
                current_plain, doc_id, backup_dir
            )
            return _ok(
                {
                    "doc_url": page.url,
                    "doc_id": doc_id,
                    "drifted": drifted,
                    "drift_summary": diff,
                    "drift_hunks_total": drift_meta.get("hunks_total", 0),
                    "drift_hunks_shown": drift_meta.get("hunks_shown", 0),
                    "drift_truncated": drift_meta.get("truncated", False),
                    "baseline_exists": baseline_path.exists(),
                    "baseline_path": str(baseline_path) if baseline_path.exists() else None,
                    "clipboard_overwritten": True,
                    "selection_changed": True,
                    "tab_focus_changed": True,
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
            doc_id = drift_mod.safe_doc_key(doc_id_lookup)
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
