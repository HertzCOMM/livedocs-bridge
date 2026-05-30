"""CLI entry point + MCP server registration.

Subcommands:
  serve          (default) Run the MCP stdio server. Used by MCP clients.
  install        One-shot install: locate Chrome, launch CDP, patch client config.
  doctor         Structured health check.
  self-test      End-to-end smoke that produces a screenshot.
  launch-chrome  Start a CDP Chrome with the managed profile.

All structured output goes to stdout as a single JSON object so LLM callers
can parse exit code + JSON without scraping. Logging goes to stderr so it
never pollutes the MCP stdio JSON-RPC stream when running as `serve`.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from . import __version__, platform_utils as pu, tools

LOG_LEVEL = os.environ.get("LIVEDOCS_LOG_LEVEL", "INFO").upper()
LOG_PATH = os.environ.get("LIVEDOCS_LOG_FILE")


def _configure_logging() -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if LOG_PATH:
        try:
            handlers.append(logging.FileHandler(LOG_PATH))
        except OSError:
            pass
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


# ---------------------------------------------------------------------------
# MCP tool registration
# ---------------------------------------------------------------------------

mcp = FastMCP("livedocs-bridge")


@mcp.tool()
async def docs_open(url: str) -> dict:
    """Open a Google Doc in the attached browser, creating it if `url` is docs.new."""
    return await tools.docs_open(url)


@mcp.tool()
async def docs_replace_all(
    content: str,
    content_type: str = "markdown",
    doc_url: Optional[str] = None,
    force: bool = False,
) -> dict:
    """Wholesale-replace the Doc body. Drift-protected + auto-backup.

    Args:
        content: Markdown or HTML payload.
        content_type: 'markdown' (default, converted to HTML) or 'html'.
        doc_url: Pin to this Doc id (RECOMMENDED). Without it, the first
            matching Doc tab is used, which can be the wrong Doc.
        force: Bypass drift abort. The Doc is still snapshotted before clearing.
    """
    return await tools.docs_replace_all(content, content_type, doc_url, force)


@mcp.tool()
async def docs_append(
    content: str,
    content_type: str = "markdown",
    doc_url: Optional[str] = None,
) -> dict:
    """Append `content` to the end of the active Doc. Snapshots the Doc first."""
    return await tools.docs_append(content, content_type, doc_url)


@mcp.tool()
async def docs_find_replace(
    find: str, replace: str, all_occurrences: bool = True
) -> dict:
    """Replace occurrences of `find` with `replace` via the Docs Find & Replace dialog."""
    return await tools.docs_find_replace(find, replace, all_occurrences)


@mcp.tool()
async def docs_screenshot(scroll_to: str = "top", path: Optional[str] = None) -> dict:
    """Capture the current Doc viewport as a PNG."""
    return await tools.docs_screenshot(scroll_to, path)


@mcp.tool()
async def docs_get_state() -> dict:
    """Return a snapshot of the active Doc's URL / title / size."""
    return await tools.docs_get_state()


@mcp.tool()
async def docs_check_drift(doc_url: Optional[str] = None) -> dict:
    """Preview whether the Doc has changed since our last push.

    Returns `{drifted, drift_summary, baseline_exists, ...}`. Use before
    `docs_replace_all` if you want to show the diff to the user instead of
    blindly triggering a drift abort.
    """
    return await tools.docs_check_drift(doc_url)


@mcp.tool()
async def docs_restore_from_backup(
    doc_url: Optional[str] = None,
    backup_timestamp: Optional[str] = None,
) -> dict:
    """Replace the Doc with a previously saved HTML backup.

    Args:
        doc_url: Doc to restore into.
        backup_timestamp: Specific backup id (format `YYYYMMDD_HHMMSS`).
            Defaults to the most recent.
    """
    return await tools.docs_restore_from_backup(doc_url, backup_timestamp)


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def _cmd_serve(_args: argparse.Namespace) -> int:
    _configure_logging()
    logging.getLogger("livedocs_bridge").info(
        "livedocs-bridge %s starting (cdp=%s)",
        __version__,
        os.environ.get("LIVEDOCS_CDP_URL", "http://127.0.0.1:19825"),
    )
    mcp.run()
    return 0


def _cmd_install(args: argparse.Namespace) -> int:
    # Local import to keep `serve` startup lean (Claude Desktop spawns this
    # subcommand on every conversation).
    from . import install as install_mod

    profile = Path(args.profile_dir).expanduser() if args.profile_dir else None
    report = install_mod.run_install(
        client=args.client,
        cdp_port=args.cdp_port,
        profile_dir=profile,
        launch_chrome=not args.no_launch,
        install_playwright=not args.no_playwright,
        json_output=args.json,
    )
    return 0 if report.success else 1


def _cmd_doctor(args: argparse.Namespace) -> int:
    from . import doctor as doctor_mod

    report = doctor_mod.run_doctor(
        cdp_url=args.cdp_url,
        client=args.client,
        json_output=args.json,
    )
    return 0 if report.overall == "healthy" else 2


def _cmd_self_test(args: argparse.Namespace) -> int:
    from . import self_test as self_test_mod

    out = Path(args.screenshot).expanduser() if args.screenshot else None
    report = self_test_mod.run_self_test(
        cdp_url=args.cdp_url,
        screenshot_path=out,
        embed_base64=args.embed_base64,
        json_output=args.json,
    )
    return 0 if report.success else 3


def _cmd_launch_chrome(args: argparse.Namespace) -> int:
    from . import install as install_mod

    info = pu.collect_platform_info()
    if info.chrome_executable is None:
        payload = {
            "tool": "livedocs-bridge",
            "command": "launch-chrome",
            "success": False,
            "error": "Could not find Google Chrome / Chromium.",
            "next_human_action": "Install Chrome from https://www.google.com/chrome/.",
        }
        print(json.dumps(payload, indent=2))
        return 1
    port = args.cdp_port if args.cdp_port is not None else pu.find_free_port()
    profile = (
        Path(args.profile_dir).expanduser() if args.profile_dir else info.default_profile_dir
    )
    profile.mkdir(parents=True, exist_ok=True)
    result = install_mod._launch_chrome(info.chrome_executable, port, profile)
    payload = {
        "tool": "livedocs-bridge",
        "version": __version__,
        "command": "launch-chrome",
        "cdp_url": f"http://127.0.0.1:{port}",
        "profile_dir": str(profile),
        "step": result.to_dict(),
        "success": result.status in ("ok", "action_required"),
    }
    print(json.dumps(payload, indent=2))
    return 0 if payload["success"] else 1


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="livedocs-bridge",
        description="Live edit Google Docs from your LLM agent via Playwright CDP.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="Run the MCP stdio server (default).")
    serve.set_defaults(func=_cmd_serve)

    install = sub.add_parser(
        "install", help="Configure an MCP client and start the CDP Chrome."
    )
    install.add_argument(
        "--client",
        choices=["claude-desktop", "cursor", "none"],
        default="claude-desktop",
        help="Which MCP client to wire (default: claude-desktop).",
    )
    install.add_argument(
        "--cdp-port", type=int, default=None, help="CDP port (default: auto)."
    )
    install.add_argument(
        "--profile-dir",
        default=None,
        help="Chrome profile dir (default: ~/.livedocs-chrome-profile).",
    )
    install.add_argument(
        "--no-launch", action="store_true", help="Don't start Chrome."
    )
    install.add_argument(
        "--no-playwright",
        action="store_true",
        help="Skip `playwright install chromium`.",
    )
    install.add_argument(
        "--json",
        dest="json",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Emit JSON to stdout (default). Use --no-json for human output.",
    )
    install.set_defaults(func=_cmd_install)

    doctor = sub.add_parser("doctor", help="Health check.")
    doctor.add_argument("--cdp-url", default=None, help="Override CDP URL to probe.")
    doctor.add_argument(
        "--client",
        choices=["claude-desktop", "cursor"],
        default="claude-desktop",
        help="Which MCP client's config to inspect.",
    )
    doctor.add_argument(
        "--json",
        dest="json",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    doctor.set_defaults(func=_cmd_doctor)

    self_test = sub.add_parser("self-test", help="End-to-end smoke + screenshot.")
    self_test.add_argument("--cdp-url", default=None)
    self_test.add_argument(
        "--screenshot",
        default=None,
        help="Write screenshot to this absolute path (default: temp file).",
    )
    self_test.add_argument(
        "--embed-base64",
        action="store_true",
        help="Also embed screenshot bytes as base64 in the JSON output.",
    )
    self_test.add_argument(
        "--json",
        dest="json",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    self_test.set_defaults(func=_cmd_self_test)

    launch = sub.add_parser(
        "launch-chrome", help="Start CDP Chrome alone (no client wiring)."
    )
    launch.add_argument("--cdp-port", type=int, default=None)
    launch.add_argument("--profile-dir", default=None)
    launch.set_defaults(func=_cmd_launch_chrome)

    return parser


def main(argv: Optional[list[str]] = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        # Default to `serve` so existing MCP client configs that call
        # `livedocs-bridge` with no args keep working.
        sys.exit(_cmd_serve(args))
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
