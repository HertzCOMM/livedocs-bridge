"""Standalone quickstart — no MCP client needed.

Edits the active Google Doc in your CDP-attached Chrome with a sample memo.
Run from a venv that has `playwright` and `markdown` installed:

    python examples/quickstart.py

Prereqs:
    1. Chrome running with `--remote-debugging-port=19825` and logged into
       the Google account that owns the target Doc.
    2. A Google Doc tab already open in that Chrome (any Doc — the script
       picks the first matching tab and replaces its contents).
"""

from __future__ import annotations

import asyncio

from livedocs_bridge.tools import docs_get_state, docs_replace_all

SAMPLE_MARKDOWN = """# livedocs-bridge demo

Hello from your LLM agent. This Doc was edited via Playwright CDP attach —
no OAuth, no SaaS, the URL never changed.

## What just happened

1. Your Chrome was already running with `--remote-debugging-port=19825`.
2. `livedocs-bridge` attached to it over CDP.
3. The active Google Doc was wiped and replaced with this markdown,
   rendered as native Docs formatting via clipboard HTML paste.

| Step | Latency |
| --- | --- |
| connect_over_cdp | <100ms |
| find or open Doc | ~1s |
| paste HTML | ~2s |

> Try it from Claude Desktop or Cursor next — same six tools, same Doc.
"""


async def main() -> None:
    print("[quickstart] replacing active Doc content...")
    result = await docs_replace_all(SAMPLE_MARKDOWN, "markdown")
    print("[quickstart] replace result:", result)
    if not result.get("success"):
        return
    state = await docs_get_state()
    print("[quickstart] state:", state)


if __name__ == "__main__":
    asyncio.run(main())
