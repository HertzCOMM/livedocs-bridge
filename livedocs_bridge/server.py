"""MCP server entry point.

Registers six tools that proxy to `livedocs_bridge.tools`. Transport is stdio
(MCP standard for local clients). All logging goes to stderr so it never
contaminates the stdio JSON-RPC stream.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Optional

from mcp.server.fastmcp import FastMCP

from . import __version__, tools

LOG_LEVEL = os.environ.get("LIVEDOCS_LOG_LEVEL", "INFO").upper()
LOG_PATH = os.environ.get("LIVEDOCS_LOG_FILE")  # optional file sink


def _configure_logging() -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if LOG_PATH:
        try:
            handlers.append(logging.FileHandler(LOG_PATH))
        except OSError:
            # File sink is best-effort; don't crash the server on a bad path.
            pass
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


mcp = FastMCP("livedocs-bridge")


@mcp.tool()
async def docs_open(url: str) -> dict:
    """Open a Google Doc in the attached browser, creating it if `url` is docs.new.

    Args:
        url: Full Doc URL like https://docs.google.com/document/d/<id>/edit,
             or "https://docs.new" / empty string to create a fresh Doc.

    Returns:
        {success, doc_url, title, tab_id} on success.
    """
    return await tools.docs_open(url)


@mcp.tool()
async def docs_replace_all(content: str, content_type: str = "markdown") -> dict:
    """Wholesale-replace the Doc body with `content`.

    Args:
        content: Markdown or HTML payload.
        content_type: 'markdown' (default, converted to HTML internally) or 'html'.

    Returns:
        {success, doc_url, chars_injected, html_bytes}.
    """
    return await tools.docs_replace_all(content, content_type)


@mcp.tool()
async def docs_append(content: str, content_type: str = "markdown") -> dict:
    """Append `content` to the end of the active Doc.

    Args:
        content: Markdown or HTML payload.
        content_type: 'markdown' (default) or 'html'.

    Returns:
        {success, doc_url, chars_appended}.
    """
    return await tools.docs_append(content, content_type)


@mcp.tool()
async def docs_find_replace(
    find: str, replace: str, all_occurrences: bool = True
) -> dict:
    """Replace occurrences of `find` with `replace` via the Docs Find & Replace dialog.

    Args:
        find: Substring to find. Must be non-empty.
        replace: Replacement substring.
        all_occurrences: If True (default), clicks "Replace all"; otherwise replaces one.

    Returns:
        {success, doc_url, replaced_count}.  `replaced_count` is -1 if Docs
        did not surface a confirmation string we could parse.
    """
    return await tools.docs_find_replace(find, replace, all_occurrences)


@mcp.tool()
async def docs_screenshot(scroll_to: str = "top", path: Optional[str] = None) -> dict:
    """Capture the current Doc viewport as a PNG.

    Args:
        scroll_to: 'top' (default), 'bottom', or 'current'.
        path: If set, write the PNG to this absolute path and return it.
              Otherwise the PNG is returned base64-encoded in `png_base64`.

    Returns:
        {success, doc_url, png_path?, png_base64?, byte_count?}.
    """
    return await tools.docs_screenshot(scroll_to, path)


@mcp.tool()
async def docs_get_state() -> dict:
    """Return a snapshot of the active Doc's URL / title / size.

    Returns:
        {success, doc_url, title, char_count, observed_at_unix}.
    """
    return await tools.docs_get_state()


def main() -> None:
    """Console-script entry point: run the MCP server over stdio."""
    _configure_logging()
    logging.getLogger("livedocs_bridge").info(
        "livedocs-bridge %s starting (cdp=%s)",
        __version__,
        os.environ.get("LIVEDOCS_CDP_URL", "http://127.0.0.1:19825"),
    )
    mcp.run()


if __name__ == "__main__":
    main()
