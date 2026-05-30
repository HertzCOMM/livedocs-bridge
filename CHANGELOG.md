# Changelog

All notable changes to `livedocs-bridge` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-05-30

Initial public release.

### Added

- MCP server (`livedocs-bridge`) exposing 6 tools over stdio:
  - `docs_open(url)` — open or focus a Google Doc in the attached browser
  - `docs_replace_all(content, content_type)` — wholesale replace Doc content (markdown or html)
  - `docs_append(content, content_type)` — append to end of Doc
  - `docs_find_replace(find, replace, all_occurrences)` — substring replace inside the Doc
  - `docs_screenshot(scroll_to, path)` — capture the editor view (top/bottom/current)
  - `docs_get_state()` — return Doc URL, title, char count
- Playwright CDP attach (`connect_over_cdp`) targeting a user-controlled Chrome
  (default `http://127.0.0.1:19825`, override via `LIVEDOCS_CDP_URL`).
- Markdown → HTML converter using `python-markdown` with `tables`, `fenced_code`,
  `nl2br` extensions so pasted content renders as native Docs formatting.
- Examples for Claude Desktop, Cursor, and Cline MCP client configs.
- Standalone `examples/quickstart.py` runnable without an MCP client.
- Smoke tests for the markdown converter and a mock-based tool test.

### Known limits

- Single-tab routing only — concurrent editing of multiple Docs is on the v0.2 roadmap.
- Requires a Chrome running with `--remote-debugging-port` and a logged-in Google account.
- DOM selector `iframe.docs-texteventtarget-iframe` could change at Google's discretion.
