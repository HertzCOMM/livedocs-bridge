"""End-to-end smoke that produces visual evidence the install actually works.

Flow:
1. Attach to user's CDP Chrome.
2. Open a brand new Doc (docs.new) in a fresh tab.
3. Inject a known marker (`✓ livedocs-bridge v<X> self-test @ <ts>`) via the
   real `docs_replace_all` tool path.
4. Screenshot the rendered Doc.
5. Read text back, verify the marker is in the canvas.
6. Print JSON with PNG path + verdict.

Caller (LLM or human) reads the JSON and shows the screenshot to the user. If
the marker is present, install is genuinely working — not just "no errors
raised". This eliminates the "LLM said it worked but didn't actually test"
failure mode.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import __version__, platform_utils as pu
from .playwright_core import (
    BrowserSession,
    clear_doc,
    find_or_open_doc,
    get_doc_text,
    get_docs_editor,
    paste_html,
    scroll_doc,
)
from .markdown_to_html import md_to_html


MARKER_TEMPLATE = "livedocs-bridge v{version} self-test @ {ts}"


@dataclass
class SelfTestReport:
    version: str
    cdp_url: str
    doc_url: Optional[str] = None
    screenshot_path: Optional[str] = None
    screenshot_base64: Optional[str] = None
    marker: Optional[str] = None
    marker_present_in_doc: bool = False
    # v0.3.3: split paste-failed vs read-back-glitch so the user (or LLM) isn't
    # sent looking at the screenshot for text that was never written.
    doc_body_chars: int = 0
    paste_landed: bool = False
    success: bool = False
    error: Optional[str] = None
    next_human_action: Optional[str] = None
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": "livedocs-bridge",
            "version": self.version,
            "command": "self-test",
            "cdp_url": self.cdp_url,
            "doc_url": self.doc_url,
            "screenshot_path": self.screenshot_path,
            "screenshot_base64": self.screenshot_base64,
            "marker": self.marker,
            "marker_present_in_doc": self.marker_present_in_doc,
            "doc_body_chars": self.doc_body_chars,
            "paste_landed": self.paste_landed,
            "success": self.success,
            "error": self.error,
            "next_human_action": self.next_human_action,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
        }


def run_self_test(
    *,
    cdp_url: Optional[str] = None,
    screenshot_path: Optional[Path] = None,
    embed_base64: bool = False,
    json_output: bool = True,
) -> SelfTestReport:
    started = time.time()
    resolved = cdp_url or os.environ.get(
        "LIVEDOCS_CDP_URL", f"http://127.0.0.1:{pu.DEFAULT_CDP_PORT}"
    )
    if not pu.cdp_endpoint_alive(resolved, timeout=2.0):
        report = SelfTestReport(
            version=__version__,
            cdp_url=resolved,
            success=False,
            error=f"CDP not reachable at {resolved}",
            next_human_action=(
                "Start the CDP Chrome with `livedocs-bridge launch-chrome`, "
                "then re-run self-test."
            ),
            elapsed_seconds=time.time() - started,
        )
        return _emit(report, json_output)

    try:
        report = asyncio.run(
            _run_async(
                cdp_url=resolved,
                screenshot_path=screenshot_path,
                embed_base64=embed_base64,
            )
        )
    except Exception as e:
        report = SelfTestReport(
            version=__version__,
            cdp_url=resolved,
            success=False,
            error=f"{type(e).__name__}: {e}",
            next_human_action=(
                "Log in to Google in the attached Chrome (the self-test tab "
                "may have landed on accounts.google.com), then re-run self-test."
            ),
        )
    report.elapsed_seconds = time.time() - started
    return _emit(report, json_output)


async def _run_async(
    *,
    cdp_url: str,
    screenshot_path: Optional[Path],
    embed_base64: bool,
) -> SelfTestReport:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    marker = MARKER_TEMPLATE.format(version=__version__, ts=ts)
    body_md = (
        f"# {marker}\n\n"
        "If you can read this in your Google Doc, livedocs-bridge is wired up "
        "correctly. This Doc was created and edited by an agent attached to "
        "your Chrome over CDP. No OAuth, no SaaS.\n\n"
        "| Check | Status |\n"
        "| --- | --- |\n"
        "| CDP attach | ✓ |\n"
        "| Frame-routed keyboard | ✓ |\n"
        "| HTML clipboard paste | ✓ |\n"
    )
    html = md_to_html(body_md)

    async with BrowserSession(cdp_url=cdp_url) as session:
        await session.grant_clipboard()
        # Always open a *new* Doc so self-test never clobbers a Doc the user cares about.
        page = await find_or_open_doc(session, "https://docs.new")
        editor = await get_docs_editor(page)
        await clear_doc(editor)
        status = await paste_html(editor, html)
        if not status.startswith("CLIP_OK"):
            return SelfTestReport(
                version=__version__,
                cdp_url=cdp_url,
                doc_url=page.url,
                marker=marker,
                success=False,
                error=f"Clipboard paste failed: {status}",
                next_human_action=(
                    "Make sure the Chrome window is in the foreground when "
                    "self-test runs (focus matters for clipboard permission), "
                    "then re-run."
                ),
            )

        # Wait briefly for Docs to ingest the paste before reading back.
        await page.wait_for_timeout(1500)
        await scroll_doc(page, "top")
        text = (await get_doc_text(page)) or ""
        marker_present = marker in text or "livedocs-bridge" in text

        png_path: Optional[str] = None
        png_b64: Optional[str] = None
        if screenshot_path:
            out = Path(screenshot_path).expanduser().resolve()
            out.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(out), full_page=False)
            png_path = str(out)
        else:
            fd, tmp = tempfile.mkstemp(prefix="livedocs_selftest_", suffix=".png")
            os.close(fd)
            await page.screenshot(path=tmp, full_page=False)
            png_path = tmp
            if embed_base64:
                png_b64 = base64.b64encode(Path(tmp).read_bytes()).decode("ascii")

        success = marker_present
        # Distinguish two failure modes when the marker isn't found:
        #   (a) The Doc body is essentially empty (just Docs' smart-chip
        #       toolbar) → the paste never landed. Most common cause on Windows
        #       prior to v0.3.3 was `Meta+V` being interpreted as Win+V
        #       (clipboard history overlay) instead of Ctrl+V.
        #   (b) The Doc has content but the read-back returned the wrong slice
        #       → canvas read-back limitation, install is actually fine.
        # The threshold (200 chars) is conservative — Docs' chip toolbar
        # contributes well under 100 chars; the self-test body is ~400+.
        body_chars = len(text.strip())
        paste_landed = body_chars >= 200 or marker_present
        if success:
            next_action = None
        elif paste_landed:
            next_action = (
                f"Doc has {body_chars} chars but the marker line was not in the "
                "read-back slice. Check the screenshot — if you can SEE the "
                "marker in the screenshot, this is a canvas read-back "
                "limitation and the install is fine."
            )
        else:
            next_action = (
                f"Doc body is essentially empty ({body_chars} chars) after the "
                "paste. The clipboard write reported OK but the Ctrl+V keystroke "
                "did not land. Verify Chrome had focus during self-test, and "
                "confirm you are on livedocs-bridge >= 0.3.3 (earlier versions "
                "used Meta+V which silently no-ops on Windows)."
            )
        return SelfTestReport(
            version=__version__,
            cdp_url=cdp_url,
            doc_url=page.url,
            screenshot_path=png_path,
            screenshot_base64=png_b64,
            marker=marker,
            marker_present_in_doc=marker_present,
            doc_body_chars=body_chars,
            paste_landed=paste_landed,
            success=success,
            next_human_action=next_action,
        )


def _emit(report: SelfTestReport, json_output: bool) -> SelfTestReport:
    payload = report.to_dict()
    if json_output:
        print(json.dumps(payload, indent=2))
    else:
        glyph = "✓" if payload["success"] else "✗"
        print(
            f"{glyph} self-test (v{payload['version']}) — "
            f"{'pass' if payload['success'] else 'fail'} "
            f"in {payload['elapsed_seconds']}s"
        )
        if payload["doc_url"]:
            print(f"  Doc: {payload['doc_url']}")
        if payload["screenshot_path"]:
            print(f"  Screenshot: {payload['screenshot_path']}")
        if payload["error"]:
            print(f"  Error: {payload['error']}")
        if payload["next_human_action"]:
            print(f"  Next: {payload['next_human_action']}")
    return report
