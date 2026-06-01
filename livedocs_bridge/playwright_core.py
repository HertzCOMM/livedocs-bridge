"""Playwright CDP attach + Google Docs frame routing helpers.

Why this exists (see docs-live-edit-playbook for full story):

Google Docs delivers real keystrokes to a *nested* iframe whose body contains
a `div[contenteditable="true"]`. A top-level page keyboard event never reaches
that handler. Playwright lets us grab `iframe.content_frame()` and dispatch
keyboard / clipboard operations against the frame-scoped element, which Docs
actually responds to.

All helpers here are async. They attach to a Chrome that the user is already
running with `--remote-debugging-port` — we never launch or close that browser.
"""

from __future__ import annotations

import asyncio
import datetime
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    ElementHandle,
    Frame,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeout,
    async_playwright,
)

DEFAULT_CDP_URL = "http://127.0.0.1:19825"
DOCS_URL_FRAGMENT = "docs.google.com/document"
DOCS_NEW_URL = "https://docs.new"
DOCS_ORIGIN = "https://docs.google.com"
EDITOR_IFRAME_SELECTOR = "iframe.docs-texteventtarget-iframe"
EDITABLE_SELECTOR = 'div[contenteditable="true"]'
SCROLL_CONTAINER_SELECTOR = ".kix-appview-editor"

# v0.3.0: Docs lazily unloads the keystroke iframe on idle tabs. The original
# 15s timeout was too tight in production. 45s + one reload retry covers the
# observed worst case (long-idle tabs, slow Docs cold start).
DEFAULT_IFRAME_TIMEOUT_MS = 45000
DEFAULT_RELOAD_TIMEOUT_MS = 30000

# v0.3.3: keyboard shortcuts MUST use Playwright's `ControlOrMeta` alias, not
# `Meta`. Playwright maps `Meta` to Win on Windows (not Ctrl), so `Meta+V`
# opens Windows clipboard history overlay instead of pasting — every keyboard
# op silently no-ops on Windows. `ControlOrMeta` resolves to Cmd on macOS and
# Ctrl on Windows/Linux, which is what we want everywhere. Available since
# Playwright 1.40 (our declared minimum).

# v0.3.4: Chrome's browser-wide CDP session corrupts after ~2-3 days of
# uptime. `connect_over_cdp` then hangs at the protocol handshake even though
# the HTTP `/json/version` probe still returns 200. Playwright default is
# 180s; we cut to 30s and surface a specific error pointing at the only
# in-band fix (kill + relaunch Chrome). Override with
# `LIVEDOCS_CDP_CONNECT_TIMEOUT_MS`.
DEFAULT_CDP_CONNECT_TIMEOUT_MS = 30000
# v0.3.5 LOW #6: clamp env-provided timeouts to a sane positive minimum.
# Sub-second connect deadlines never produce useful CDP sessions and the
# negative-int path was previously silently passed through to Playwright.
MIN_CDP_CONNECT_TIMEOUT_MS = 1000


def get_cdp_connect_timeout_ms() -> int:
    raw = os.environ.get("LIVEDOCS_CDP_CONNECT_TIMEOUT_MS")
    if not raw:
        return DEFAULT_CDP_CONNECT_TIMEOUT_MS
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_CDP_CONNECT_TIMEOUT_MS
    if parsed < MIN_CDP_CONNECT_TIMEOUT_MS:
        return DEFAULT_CDP_CONNECT_TIMEOUT_MS
    return parsed


def get_cdp_url() -> str:
    """Resolve the CDP endpoint from env (LIVEDOCS_CDP_URL) or default."""
    return os.environ.get("LIVEDOCS_CDP_URL", DEFAULT_CDP_URL)


class CDPConnectTimeout(RuntimeError):
    """Raised when `connect_over_cdp` hangs past the configured deadline.

    Concrete signal that the user should kill + relaunch Chrome instead of
    waiting another 150 seconds for the default Playwright timeout.
    """


@dataclass
class DocsEditor:
    """A bundle of handles we need to drive a Doc."""

    page: Page
    iframe_handle: ElementHandle
    frame: Frame
    editable: Locator


class BrowserSession:
    """Long-lived Playwright session attached to user's Chrome via CDP.

    Use as an async context manager *or* call `start()` / `stop()` directly.
    The underlying Chrome is owned by the user — we only borrow it.
    """

    def __init__(self, cdp_url: Optional[str] = None) -> None:
        self.cdp_url = cdp_url or get_cdp_url()
        self._pw = None
        self._browser: Optional[Browser] = None
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "BrowserSession":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.stop()

    async def start(self) -> None:
        if self._browser is not None:
            return
        self._pw = await async_playwright().start()
        timeout_ms = get_cdp_connect_timeout_ms()
        try:
            self._browser = await self._pw.chromium.connect_over_cdp(
                self.cdp_url, timeout=timeout_ms
            )
        except PlaywrightTimeout as e:
            # Tear down the playwright instance so the next call gets a fresh
            # one instead of inheriting a half-started state.
            await self._pw.stop()
            self._pw = None
            # v0.3.5 LOW #5: generic recovery first, then the
            # livedocs-bridge example. bb-browser users, container daemons
            # and remote-host setups don't drive Chrome via launch-chrome.
            raise CDPConnectTimeout(
                f"connect_over_cdp({self.cdp_url}) timed out after "
                f"{timeout_ms / 1000:.1f}s. This usually means the Chrome "
                f"browser-wide CDP session has corrupted (typical after 2-3 "
                f"days of uptime). The HTTP /json/version probe may still "
                f"return 200 but the protocol handshake hangs. "
                f"Recovery: restart the Chrome instance backing this CDP "
                f"endpoint — kill the process and re-launch with the same "
                f"`--remote-debugging-port` + `--user-data-dir`. "
                f"If livedocs-bridge owns the Chrome process, "
                f"`livedocs-bridge launch-chrome` will respawn it. "
                f"`user-data-dir` is persistent so your Google login survives."
            ) from e

    async def stop(self) -> None:
        # We never close the browser — it belongs to the user.
        if self._pw is not None:
            try:
                await self._pw.stop()
            finally:
                self._pw = None
                self._browser = None

    @property
    def browser(self) -> Browser:
        if self._browser is None:
            raise RuntimeError("BrowserSession not started")
        return self._browser

    def primary_context(self) -> BrowserContext:
        ctxs = self.browser.contexts
        if not ctxs:
            raise RuntimeError("No browser contexts available over CDP")
        return ctxs[0]

    async def grant_clipboard(self, context: Optional[BrowserContext] = None) -> None:
        ctx = context or self.primary_context()
        try:
            await ctx.grant_permissions(
                ["clipboard-read", "clipboard-write"],
                origin=DOCS_ORIGIN,
            )
        except Exception:
            # Some CDP-attached contexts don't allow grant_permissions; the
            # paste path will still work if the browser already trusts the
            # origin (user has previously allowed clipboard).
            pass


async def find_or_open_doc(
    session: BrowserSession,
    url: Optional[str] = None,
    *,
    wait_for_editor: bool = True,
    timeout_ms: int = 20000,
) -> Page:
    """Locate an existing Doc tab matching `url`, else open one.

    Args:
        session: Live BrowserSession.
        url: Full Doc URL, or None / "docs.new" to create a fresh Doc.
        wait_for_editor: If True, wait until the editor iframe mounts.
        timeout_ms: Max wait for editor iframe.

    Returns:
        The Page handle pointing at the Doc.
    """
    ctx = session.primary_context()
    target = url or DOCS_NEW_URL

    needle = _doc_match_key(target)
    if needle:
        for page in ctx.pages:
            if needle in page.url:
                await page.bring_to_front()
                if wait_for_editor:
                    await page.wait_for_selector(
                        EDITOR_IFRAME_SELECTOR, timeout=timeout_ms
                    )
                return page

    page = await ctx.new_page()
    try:
        await page.goto(
            target,
            wait_until="domcontentloaded",
            timeout=DEFAULT_RELOAD_TIMEOUT_MS,
        )
    except PlaywrightTimeout:
        # Docs polls forever; even domcontentloaded can timeout on cold start.
        # The editor selector wait below is the real readiness gate.
        print(
            "  goto timed out at domcontentloaded; proceeding anyway",
            file=sys.stderr,
        )
    await page.bring_to_front()
    if wait_for_editor:
        await page.wait_for_selector(EDITOR_IFRAME_SELECTOR, timeout=timeout_ms)
    return page


def _doc_match_key(url: str) -> Optional[str]:
    """Extract a stable substring to match against open tab URLs.

    For "https://docs.google.com/document/d/ABC/edit" we want "/document/d/ABC".
    For "https://docs.new" (no doc id yet) we return None so the caller opens
    a fresh tab instead of reusing any existing Doc.
    """
    if "/document/d/" in url:
        head, _, tail = url.partition("/document/d/")
        doc_id = tail.split("/", 1)[0].split("?", 1)[0]
        if doc_id:
            return f"/document/d/{doc_id}"
    if "docs.new" in url:
        return None
    if DOCS_URL_FRAGMENT in url:
        return DOCS_URL_FRAGMENT
    return None


async def get_docs_editor(
    page: Page,
    *,
    timeout_ms: int = DEFAULT_IFRAME_TIMEOUT_MS,
    allow_reload: bool = True,
) -> DocsEditor:
    """Resolve the nested iframe + contenteditable for typing into a Doc.

    Hardened for long-idle tabs: if the keystroke iframe doesn't mount within
    `timeout_ms`, we reload the page once (domcontentloaded, not networkidle —
    Docs polls forever and networkidle never settles) and retry the selector.
    A single reload is enough in practice; two would just stall.
    """
    await page.bring_to_front()
    await page.wait_for_timeout(500)

    try:
        await page.wait_for_selector(EDITOR_IFRAME_SELECTOR, timeout=timeout_ms)
    except PlaywrightTimeout:
        if not allow_reload:
            raise
        print(
            f"  iframe not ready in {timeout_ms}ms, reloading page once...",
            file=sys.stderr,
        )
        try:
            await page.reload(
                wait_until="domcontentloaded", timeout=DEFAULT_RELOAD_TIMEOUT_MS
            )
        except PlaywrightTimeout:
            print(
                "  reload timed out at domcontentloaded; proceeding to retry selector",
                file=sys.stderr,
            )
        await page.wait_for_timeout(2000)
        await page.wait_for_selector(EDITOR_IFRAME_SELECTOR, timeout=timeout_ms)

    iframe_locator = page.locator(EDITOR_IFRAME_SELECTOR)
    iframe_handle = await iframe_locator.element_handle()
    if iframe_handle is None:
        raise RuntimeError("Could not resolve Docs editor iframe handle")
    frame = await iframe_handle.content_frame()
    if frame is None:
        raise RuntimeError("Could not resolve Docs editor content frame")
    editable = frame.locator(EDITABLE_SELECTOR).first
    await editable.wait_for(timeout=10000)
    await editable.focus()
    return DocsEditor(
        page=page, iframe_handle=iframe_handle, frame=frame, editable=editable
    )


async def clear_doc(editor: DocsEditor) -> None:
    """Select-all + delete to wipe the Doc body.

    SAFETY: callers that are about to wholesale-replace user content MUST
    invoke `backup_doc(...)` and `drift.check_drift(...)` first. clear_doc is
    destructive and silently overwrites any manual edits the user has made
    since the last push.
    """
    await editor.editable.focus()
    await editor.page.keyboard.press("ControlOrMeta+A")
    await editor.page.wait_for_timeout(150)
    await editor.page.keyboard.press("Backspace")
    await editor.page.wait_for_timeout(300)


async def capture_doc_plain(editor: DocsEditor) -> str:
    """Capture the current Doc as plain text via Cmd+A + Cmd+C + clipboard.readText.

    Used both before destructive ops (to compute drift) and after a successful
    push (to refresh the baseline saved via `drift.save_last_push`).
    """
    await editor.editable.focus()
    await editor.page.keyboard.press("ControlOrMeta+A")
    await editor.page.wait_for_timeout(300)
    await editor.page.keyboard.press("ControlOrMeta+C")
    await editor.page.wait_for_timeout(600)
    js = """
    async () => {
      try { return await navigator.clipboard.readText(); }
      catch (e) { return ''; }
    }
    """
    try:
        result = await editor.page.evaluate(js)
    except Exception as e:  # noqa: BLE001 — diagnostic only
        print(f"  capture_doc_plain failed: {e}", file=sys.stderr)
        return ""
    return result or ""


async def backup_doc(
    editor: DocsEditor,
    backup_dir: Path,
    doc_url_or_id: Optional[str] = None,
) -> dict:
    """Snapshot Doc to backup_dir BEFORE any destructive op.

    Uses Cmd+A + Cmd+C + clipboard.read() so we capture both text/plain (for
    drift comparison) and text/html (for restore). Writes
    `doc_backup_<ts>_<short_id>.{txt,html}` and returns the resolved paths.

    Failure modes are non-fatal — we still return a dict, but with None entries
    and a warning logged to stderr. Backups are advisory, not a transaction.
    """
    from . import drift as drift_mod  # local import to avoid cycle at import time

    backup_dir = Path(backup_dir).expanduser()
    backup_dir.mkdir(parents=True, exist_ok=True)

    page = editor.page
    target_id = doc_url_or_id or page.url

    await editor.editable.focus()
    await page.keyboard.press("ControlOrMeta+A")
    await page.wait_for_timeout(300)
    await page.keyboard.press("ControlOrMeta+C")
    await page.wait_for_timeout(600)

    js = """
    async () => {
      const out = {};
      try {
        const items = await navigator.clipboard.read();
        for (const item of items) {
          for (const type of item.types) {
            try {
              const blob = await item.getType(type);
              out[type] = await blob.text();
            } catch (e) { /* skip individual type */ }
          }
        }
        if (Object.keys(out).length === 0) {
          out['text/plain'] = await navigator.clipboard.readText();
        }
        return out;
      } catch (e) {
        try {
          return {
            'text/plain': await navigator.clipboard.readText(),
            '_warn': 'clipboard.read failed, fell back to readText: ' + e.message,
          };
        } catch (e2) {
          return {_error: e.message + ' / readText: ' + e2.message};
        }
      }
    }
    """
    data = await page.evaluate(js) or {}

    base = drift_mod.backup_base_path(backup_dir, target_id)
    result: dict = {
        "txt": None,
        "html": None,
        "doc_url": page.url,
        "doc_id": drift_mod.extract_doc_id(target_id) or drift_mod.extract_doc_id(page.url),
        "warning": None,
        "error": None,
    }

    if "_error" in data:
        result["error"] = data["_error"]
        print(f"  backup FAILED: {data['_error']}", file=sys.stderr)
        return result
    if "_warn" in data:
        result["warning"] = data["_warn"]
        print(f"  backup partial: {data['_warn']}", file=sys.stderr)

    plain = data.get("text/plain", "") or ""
    html = data.get("text/html", "") or ""

    if plain:
        txt_path = base.with_suffix(".txt")
        drift_mod.atomic_write_text(txt_path, plain)
        result["txt"] = txt_path
    if html:
        html_path = base.with_suffix(".html")
        drift_mod.atomic_write_text(html_path, html)
        result["html"] = html_path

    if result["txt"] or result["html"]:
        paths = ", ".join(str(p) for p in (result["txt"], result["html"]) if p)
        print(f"  📦 backup: {paths}", file=sys.stderr)
    else:
        print("  backup empty (Doc may be empty)", file=sys.stderr)
    return result


async def move_caret_to_end(editor: DocsEditor) -> None:
    await editor.editable.focus()
    await editor.page.keyboard.press("ControlOrMeta+End")
    await editor.page.wait_for_timeout(100)


async def insert_text(editor: DocsEditor, text: str) -> None:
    """Fast raw text insertion using CDP `Input.insertText`."""
    await editor.editable.focus()
    await editor.page.keyboard.insert_text(text)


async def paste_html(editor: DocsEditor, html: str) -> str:
    """Write HTML to clipboard via page.evaluate, then Cmd+V.

    Returns the literal status string from the clipboard write so callers
    can surface it as an error message on failure.
    """
    js = """
    async (html) => {
      try {
        const blob = new Blob([html], {type: 'text/html'});
        const txt = new Blob([html.replace(/<[^>]+>/g, '')], {type: 'text/plain'});
        await navigator.clipboard.write([new ClipboardItem({
          'text/html': blob,
          'text/plain': txt,
        })]);
        return 'CLIP_OK';
      } catch (e) {
        return 'CLIP_ERR ' + e.name + ': ' + e.message;
      }
    }
    """
    await editor.page.bring_to_front()
    await editor.editable.focus()
    status = await editor.page.evaluate(js, html)
    if not isinstance(status, str) or not status.startswith("CLIP_OK"):
        return status if isinstance(status, str) else "CLIP_ERR unknown"
    await editor.editable.focus()
    await editor.page.keyboard.press("ControlOrMeta+V")
    await editor.page.wait_for_timeout(1500)
    return status


async def scroll_doc(page: Page, where: str) -> None:
    """Scroll the Docs canvas. `where`: 'top' | 'bottom' | 'current'."""
    if where == "current":
        return
    js_top = """
    () => {
      const el = document.querySelector('.kix-appview-editor');
      if (el) el.scrollTop = 0; else window.scrollTo(0, 0);
    }
    """
    js_bottom = """
    () => {
      const el = document.querySelector('.kix-appview-editor');
      if (el) el.scrollTop = el.scrollHeight; else window.scrollTo(0, document.body.scrollHeight);
    }
    """
    if where == "top":
        await page.evaluate(js_top)
    elif where == "bottom":
        await page.evaluate(js_bottom)
    else:
        raise ValueError(f"Unknown scroll_to: {where!r}")
    await page.wait_for_timeout(400)


async def get_doc_text(page: Page) -> str:
    """Best-effort plain-text grab from the rendered editor canvas.

    Docs renders body content to canvas, so DOM `textContent` is the most
    reliable available source for find-replace and char counts. Not suitable
    for exact fidelity but good enough for substring matching.
    """
    return await page.evaluate(
        """
        () => {
          const el = document.querySelector('.kix-appview-editor');
          return el ? el.innerText : document.body.innerText;
        }
        """
    )


async def get_doc_title(page: Page) -> str:
    """Pull the Doc title from the title bar input if available, fall back to page title."""
    try:
        title = await page.evaluate(
            """
            () => {
              const el = document.querySelector('input.docs-title-input');
              if (el && el.value) return el.value;
              return document.title;
            }
            """
        )
        if isinstance(title, str):
            return title
    except Exception:
        pass
    return await page.title()
