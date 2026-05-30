# Changelog

All notable changes to `livedocs-bridge` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-05-30

LLM-driven install: four new subcommands that turn "user runs five CLI
commands and hand-edits JSON" into "user tells their agent to install."

### Added

- `livedocs-bridge install` — one-shot, idempotent install for Claude Desktop
  or Cursor. Locates Chrome, ensures Playwright chromium, finds a free CDP
  port, launches Chrome detached, atomically patches MCP client config with
  a timestamped backup, probes the CDP endpoint, prints the single concrete
  next action the user has to take.
- `livedocs-bridge doctor` — structured health check (`--json` by default)
  covering PATH, Chrome install, CDP reachability, client config wiring, and
  profile dir. Every failed check carries a one-line `fix` field.
- `livedocs-bridge self-test` — opens a fresh Doc, paste-injects a versioned
  marker, screenshots, reads back to verify the marker actually rendered.
  Output is JSON with screenshot path + verdict so an LLM can show evidence
  to the user instead of claiming success blindly.
- `livedocs-bridge launch-chrome` — start the managed CDP Chrome without
  touching any client config.
- `livedocs_bridge.platform_utils` — OS / Chrome / config path resolution
  + port probe helpers used by the new commands.

### Changed

- CLI is now a subcommand dispatcher. `livedocs-bridge` with no args still
  runs the MCP stdio server (`serve`), so existing client configs keep working.
- The MCP client config written by `install` now uses an explicit
  `args: ["serve"]` so the entry is unambiguous when read back by `doctor`.

### Why

v0.1 install was 11 manual steps; LLM-driven install was 7. Both had ~5
silent failure modes (wrong python, malformed JSON, port conflict, missing
PATH, wrong OS path). v0.2 collapses install to one idempotent command with
structured output. LLMs branch on exit code + JSON, not log scraping.

## [0.1.0] - 2026-05-30

Initial public release.

### Added

- MCP server (`livedocs-bridge`) exposing 6 tools over stdio:
  `docs_open`, `docs_replace_all`, `docs_append`, `docs_find_replace`,
  `docs_screenshot`, `docs_get_state`.
- Playwright CDP attach targeting a user-controlled Chrome
  (default `http://127.0.0.1:19825`, override via `LIVEDOCS_CDP_URL`).
- Markdown → HTML converter using `python-markdown` with `tables`, `fenced_code`,
  `nl2br` extensions.
- Examples for Claude Desktop, Cursor, Cline.
- Standalone `examples/quickstart.py` runnable without an MCP client.
- Mock-based smoke tests for tool wrappers and the markdown converter.
