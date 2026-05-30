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
import os
from dataclasses import dataclass
from typing import Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    ElementHandle,
    Frame,
    Locator,
    Page,
    async_playwright,
)

DEFAULT_CDP_URL = "http://127.0.0.1:19825"
DOCS_URL_FRAGMENT = "docs.google.com/document"
DOCS_NEW_URL = "https://docs.new"
DOCS_ORIGIN = "https://docs.google.com"
EDITOR_IFRAME_SELECTOR = "iframe.docs-texteventtarget-iframe"
EDITABLE_SELECTOR = 'div[contenteditable="true"]'
SCROLL_CONTAINER_SELECTOR = ".kix-appview-editor"


def get_cdp_url() -> str:
    """Resolve the CDP endpoint from env (LIVEDOCS_CDP_URL) or default."""
    return os.environ.get("LIVEDOCS_CDP_URL", DEFAULT_CDP_URL)


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
        self._browser = await self._pw.chromium.connect_over_cdp(self.cdp_url)

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
    await page.goto(target, wait_until="domcontentloaded")
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


async def get_docs_editor(page: Page, *, timeout_ms: int = 15000) -> DocsEditor:
    """Resolve the nested iframe + contenteditable for typing into a Doc."""
    await page.wait_for_selector(EDITOR_IFRAME_SELECTOR, timeout=timeout_ms)
    iframe_locator = page.locator(EDITOR_IFRAME_SELECTOR)
    iframe_handle = await iframe_locator.element_handle()
    if iframe_handle is None:
        raise RuntimeError("Could not resolve Docs editor iframe handle")
    frame = await iframe_handle.content_frame()
    if frame is None:
        raise RuntimeError("Could not resolve Docs editor content frame")
    editable = frame.locator(EDITABLE_SELECTOR).first
    await editable.wait_for(timeout=timeout_ms)
    await editable.focus()
    return DocsEditor(
        page=page, iframe_handle=iframe_handle, frame=frame, editable=editable
    )


async def clear_doc(editor: DocsEditor) -> None:
    """Select-all + delete to wipe the Doc body."""
    await editor.editable.focus()
    await editor.page.keyboard.press("Meta+A")
    await editor.page.wait_for_timeout(150)
    await editor.page.keyboard.press("Backspace")
    await editor.page.wait_for_timeout(300)


async def move_caret_to_end(editor: DocsEditor) -> None:
    await editor.editable.focus()
    await editor.page.keyboard.press("Meta+End")
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
    await editor.page.keyboard.press("Meta+V")
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
