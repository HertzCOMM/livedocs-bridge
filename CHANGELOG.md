# Changelog

All notable changes to `livedocs-bridge` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.1] - 2026-05-31

Codex adversarial audit (gpt-5.3-codex) on the v0.3.0 diff surfaced 1 CRITICAL,
3 HIGH, 2 MEDIUM, 2 LOW. All except the prune-future-mtime and contexts[0]
items are fixed here. v0.3.0 should be considered superseded.

### Fixed

- **[CRITICAL] Cross-Doc backup collision.** v0.3.0 truncated the Google Doc id
  to 16 chars when naming `doc_backup_*` files. Two distinct Docs sharing a
  16-char prefix collided, and `docs_restore_from_backup` could pick the
  *other* Doc's backup and overwrite the current Doc with it. The truncation
  is gone — both baseline and timestamped backups now use the full safe id.
- **[HIGH] Pre-op capture failure fail-open.** If the pre-op clipboard read
  failed silently (focus lost, permission revoked), `current_plain` became
  `""`, drift compared to an empty baseline as "no drift", and a destructive
  replace proceeded without a trustworthy snapshot. `docs_replace_all` now
  fails closed with `capture_failed: true` unless the caller passes
  `force=True`.
- **[HIGH] Non-atomic baseline writes.** `_last_pushed_*` and `doc_backup_*`
  files were written with plain `Path.write_text`. A crash mid-write left
  truncated content that the next drift check would silently mistake for the
  prior baseline. All baseline + backup writes now go through temp file +
  `os.replace`.
- **[HIGH] TOCTOU window between snapshot and clear.** `docs_replace_all`
  now does a second clipboard recapture immediately before `clear_doc`. If
  the user landed an edit in the meantime, the call aborts with
  `toctou_detected: true` unless `force=True`.
- **[MEDIUM] Path traversal via crafted `doc_url`.** Doc ids that didn't
  match `[A-Za-z0-9_-]{1,128}` were written into the filename verbatim,
  including `/` and `..`. They are now hashed to `h_<sha256[:32]>` so the
  baseline / backup path always stays inside the backup directory.
- **[MEDIUM] `docs_check_drift` undeclared side effects.** The tool runs
  Cmd+A + Cmd+C, which changes selection and overwrites the clipboard with
  the Doc body. The docstring now says so loudly and the response includes
  `clipboard_overwritten: true` + `selection_changed: true`.
- **[LOW] Silent data-loss exposure on post-clear paste failure.** When
  `paste_html` failed after `clear_doc` already wiped the Doc, the response
  carried a generic clipboard error. It now sets `doc_may_be_empty: true`
  and `recommended_next_action: "docs_restore_from_backup"`.

### Added

- `drift.safe_doc_key(url_or_id)` — canonical id sanitizer used everywhere.
- `drift.atomic_write_text(path, data)` — temp + `os.replace` helper.
- 17 new regression tests, one per audit finding (87 total).

### Acknowledged but not fixed in v0.3.1

- **[LOW] Future-dated `doc_backup_*` files never prune.** Acceptable risk
  for single-user installs; will revisit if anyone reports it.
- **`contexts[0]` bias.** Multi-context Chrome attach picks the first
  context only. Same single-user assumption; v0.4 candidate alongside
  multi-tab routing.

## [0.3.0] - 2026-05-31

Production hardening driven by an 8-hour, 20-iteration real workflow (HertzFlow
× WLFI investment memo live edit). Every change below maps to a concrete
incident that broke the v0.2.0 happy path.

### Added

- **Drift detection.** Before any destructive write, the Doc is snapshotted
  and compared against the last push baseline. If they don't match the user
  has edited the Doc under us — `docs_replace_all` aborts with the diff
  unless the caller passes `force=True`.
- **Persistent auto-backup.** Every destructive op writes `text/plain` and
  `text/html` snapshots to `~/.livedocs-bridge/backups/`
  (override with `LIVEDOCS_BACKUP_DIR`). Survives macOS reboots — `/tmp`
  did not. Backups older than 30 days are pruned automatically; baselines
  are never pruned.
- **`docs_check_drift(doc_url?)` MCP tool.** Preview drift without committing
  to a replace. Useful when you want to show the diff to the user before
  asking to force.
- **`docs_restore_from_backup(doc_url?, backup_timestamp?)` MCP tool.**
  Recovery path: restore the Doc from the latest (or a specific) backup.
  Snapshots the pre-restore state first so an erroneous restore can be undone.
- **`doc_url` argument on `docs_replace_all` and `docs_append`.** Pin to a
  specific Doc id — without it, the first matching Doc tab is used, which
  can be the wrong Doc in multi-tab sessions.
- **Iframe reload retry.** `get_docs_editor` now waits 45 s for the
  `docs-texteventtarget-iframe` (3× the prior 15 s) and reloads the page once
  on timeout. Long-idle Docs tabs lazily unload that iframe; the retry catches
  it without surfacing the failure to the caller.
- **`livedocs_bridge.drift` module** with the full set of helpers:
  `extract_doc_id`, `save_last_push`, `check_drift`, `prune_old_backups`,
  `list_backups`, `find_backup`, `default_backup_dir`.

### Changed

- `find_or_open_doc` and `get_docs_editor` use `wait_until="domcontentloaded"`
  instead of `networkidle`. Docs polls forever — `networkidle` never settles.
- The drift baseline filename uses the full Doc id (`_last_pushed_<id>.txt`);
  timestamped backups truncate the id to 16 chars to keep filenames short on
  Windows.
- `docs_replace_all` return shape now includes `drift_detected`,
  `drift_summary`, `backup_paths`, `baseline_saved`, `baseline_path`, `forced`.
- `docs_append` snapshots the Doc before appending (no drift abort — append
  is non-destructive — but the backup gives you a recovery path).

### Documentation

- README now includes the 10 production gotchas observed during the live-edit
  session, including the Chrome long-uptime CDP-handshake hang that has no
  in-band fix (kill + relaunch).

### Migration notes

- Existing `docs_replace_all(content, content_type)` callers keep working;
  `doc_url` and `force` are optional. The new return-shape fields are
  additive.
- The default backup directory is `~/.livedocs-bridge/backups/`. If you want
  to retain backups from the internal skill, set
  `LIVEDOCS_BACKUP_DIR=~/.claude/google-doc-editing-backups`.

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
