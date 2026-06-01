# Changelog

All notable changes to `livedocs-bridge` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.6] - 2026-06-01

Codex verification audit on v0.3.5 confirmed 0 new CRITICAL/HIGH and gave a
clean M41 verdict. Two MEDIUM-level "partial" residuals were flagged and
are closed here. (The third partial, "difflib still splits inputs ≤ 2 MiB
internally", is pure efficiency overhead, not data safety; accepted.)

### Fixed

- **[MEDIUM #2 residual] 80-char empty-capture threshold was hardcoded.**
  Some Docs locales inject longer smart-chip boilerplate, which would
  false-fail the empty-source verification path. v0.3.6 makes the
  threshold env-tunable via `LIVEDOCS_VERIFY_EMPTY_CAPTURE_MAX_CHARS`
  (default 80). The response meta now carries `empty_capture_threshold`
  so callers can confirm which value applied.
- **[MEDIUM #3 residual] Fingerprint dedup defeated multi-fingerprint defense.**
  When the 3 spread offsets landed on identical content (e.g. user pasted
  the same paragraph multiple times), `chunk not in out` collapsed the
  list to 1 fingerprint, and the 2-of-3 match requirement degraded to
  1-of-1. v0.3.6 drops the dedup, preserves duplicates, and counts
  occurrences with `collections.Counter`. A repeated chunk now requires
  the capture to contain it ≥ N times to count as matched. New meta field
  `fingerprint_expected_counts` exposes the per-chunk occurrence target.

### Added

- `LIVEDOCS_VERIFY_EMPTY_CAPTURE_MAX_CHARS` env var.
- `livedocs_bridge.tools._empty_capture_max_chars` helper.
- `paste_verification_meta.empty_capture_threshold` and
  `paste_verification_meta.fingerprint_expected_counts` response fields.
- 8 new regression tests (149 total).

### Acknowledged residuals

- **HIGH #1 internal difflib work**: `difflib.unified_diff` still does
  full diff work on inputs up to the 2 MiB cap. Pure efficiency, not a
  safety issue. The 2 MiB cap already bounds memory; the streaming
  iterator already bounds output. Accepted.
- **LOW #6 sub-1000ms timeout policy**: by-design (clamping intentionally
  rejects sub-second connect deadlines as policy, not bug).

## [0.3.5] - 2026-06-01

Codex adversarial audit on the v0.3.4 patches surfaced 1 HIGH, 4 MEDIUM,
2 LOW. All fixed.

### Fixed

- **[HIGH] Unbounded diff materialization (OOM risk).** `check_drift` did
  `list(unified_diff(...))` against the full baseline + current text. A
  multi-MB Doc body would allocate hundreds of MB of diff lines before our
  line cap truncated for display. v0.3.5: inputs > 2 MB per side trigger
  `drifted=True` with a "too large to diff" summary (override via
  `LIVEDOCS_DRIFT_MAX_INPUT_BYTES`); the diff iterator is consumed with
  `itertools.islice(DRIFT_HARD_LINE_CAP=5000)` so we never materialize more
  than 5K lines even when the line cap is raised.
- **[MEDIUM] Empty-source paste auto-verified.** An empty `content` made
  `_verify_paste_landed` short-circuit to `verified=True` without proving
  the paste landed. A stale capture could then be saved as the new baseline.
  v0.3.5: empty source requires the post-paste capture to also be empty
  (≤ 80 chars of smart-chip boilerplate); otherwise the paste didn't truly
  clear-and-replace, and the response fails closed with `empty_source: true`
  in the verification meta.
- **[MEDIUM] Low-entropy fingerprint false positives.** A repetitive source
  like `"A" * 500` yielded a `"AAA"` fingerprint that matched almost any
  Doc body. v0.3.5: picks up to 3 spread-out fingerprints, requires ≥ 2 to
  match, and flags `low_entropy_fingerprint: true` when the combined
  fingerprints have < 6 distinct alphanumeric characters so callers know
  the result is weaker evidence than usual.
- **[MEDIUM] Misleading recovery advice on verification failure.** The
  previous message told the user "re-run docs_replace_all (drift will catch
  real divergence)" — but the baseline was NOT saved, so re-run hits the
  "no baseline → first inject" path and drift returns False regardless of
  Doc content. v0.3.5: response now carries `drift_protection_reduced: true`
  and a 3-step actionable message (screenshot → restore or accept reduced
  protection until next verified push).
- **[LOW] CDP timeout message too prescriptive.** The message named
  `livedocs-bridge launch-chrome` as the only recovery path, but bb-browser
  users / managed daemons / remote-host setups don't use it. v0.3.5: generic
  recovery first ("restart the Chrome instance backing this CDP endpoint —
  same `--remote-debugging-port` + `--user-data-dir`"), then optional
  `launch-chrome` example for self-managed setups.
- **[LOW] Negative env timeout silently accepted.** `LIVEDOCS_CDP_CONNECT_
  TIMEOUT_MS=-5000` parsed cleanly and was passed straight to Playwright.
  v0.3.5: values below `MIN_CDP_CONNECT_TIMEOUT_MS=1000` snap back to the
  default.

### Added

- `LIVEDOCS_DRIFT_MAX_INPUT_BYTES` env var (default 2 MiB per side).
- `livedocs_bridge.tools._pick_fingerprints` / `_distinct_alnum_chars` helpers.
- 19 new regression tests (141 total).

### Notes

- All v0.3.4 functionality preserved; this is a hardening release. v0.3.4
  callers continue to work, with three response-field additions:
  `drift_protection_reduced`, `drift_truncated`, and `paste_verification_meta`
  shape now includes `fingerprints` / `fingerprints_matched` / `empty_source` /
  `low_entropy_fingerprint`.

## [0.3.4] - 2026-06-01

Three production bugs from real workflow use of the underlying skill (see
report from 2026-06-01 session). All three are now fixed in the MCP wrapper.

### Fixed

- **[Bug 1] Drift summary truncation silently hid hunks.** When a Doc had
  edits in multiple sections, `check_drift` returned only the first 80 diff
  lines. An agent reading the response saw the §6 hunk and assumed it was
  the only drift; on `force=True` the §4 hunks were silently overwritten.
  `check_drift` now returns a 3-tuple `(drifted, summary, meta)` with
  `meta["hunks_total"] / hunks_shown / lines_total / lines_shown / truncated`.
  When truncated, a loud `⚠ DRIFT TRUNCATED: showing N of M hunks; force=True
  overwrites ALL drift including the hidden hunks` banner is prepended to
  the summary. Default cap raised 80 → 200 lines. Both `docs_replace_all`
  and `docs_check_drift` responses now surface `drift_hunks_total` and
  `drift_truncated`.
- **[Bug 2] Silent paste failure + corrupted baseline.** Real incident:
  user added a sentence to a memo, ran replace, script reported DONE, the
  addition was missing from the Doc AND from the saved baseline. The
  post-paste `Cmd+A + Cmd+C` capture either ran during render lag or never
  saw the paste land — but the empty/old capture was saved as the new
  baseline anyway. Next inject saw "drift" (user's real edits vs corrupted
  baseline) and `force=True` overwrote them. `docs_replace_all` now extracts
  a 60-char fingerprint from the source content (HTML-stripped, whitespace-
  collapsed), verifies it appears in the post-paste capture, retries once
  with a 2.5 s wait on miss, and **refuses to save the baseline** when
  verification ultimately fails. Response carries `paste_verification_failed:
  true` + `paste_verification_meta` + a concrete `recommended_next_action`
  (screenshot first, then restore-from-backup if visually wrong).
- **[Bug 3] CDP connect hung 180 s on corrupted Chrome session.** After
  ~2-3 days of Chrome uptime, `connect_over_cdp` hangs at the protocol
  handshake even though `/json/version` still returns 200. v0.3.4 caps the
  connect timeout at 30 s (override via `LIVEDOCS_CDP_CONNECT_TIMEOUT_MS`)
  and raises a new `CDPConnectTimeout` with a concrete recovery path
  (kill + relaunch via `livedocs-bridge launch-chrome`; user-data-dir is
  persistent so login survives). No more 180 s silent stalls.

### Changed

- `livedocs_bridge.drift.check_drift` return type changed from
  `tuple[bool, str]` → `tuple[bool, str, dict]`. Internal callers updated.
  External callers (if any) must unpack 3 values.
- Default `check_drift(diff_max_lines=...)` raised 80 → 200.

### Added

- `LIVEDOCS_CDP_CONNECT_TIMEOUT_MS` env var (default 30000 ms).
- `livedocs_bridge.playwright_core.CDPConnectTimeout` exception class.
- `livedocs_bridge.tools._content_to_verification_plain` /
  `_pick_fingerprint` / `_verify_paste_landed` helpers.
- 16 new regression tests (122 total).

### Notes

- v0.3.4 is **strongly recommended** for anyone using `docs_replace_all` —
  Bug 2 silently corrupted baselines on every Docs render lag spike and the
  next inject would silently overwrite user edits.

## [0.3.3] - 2026-06-01

Windows / Linux keyboard fix. v0.3.2 and earlier hardcoded `Meta+<key>` for
every keyboard op. Playwright maps `Meta` to **Win** on Windows (not Ctrl), so
`Meta+V` opened the Windows clipboard history overlay instead of pasting, and
every other op (`Meta+A`, `Meta+C`, `Meta+End`, `Meta+Shift+H`) silently
no-opped. Reported by a Windows user running a clean install of v0.3.2 — see
GitHub issue for the full repro.

### Fixed

- **Windows / Linux paste, select-all, copy, end-of-doc.** All seven keyboard
  ops in `playwright_core.py` (`clear_doc`, `capture_doc_plain` ×2,
  `backup_doc` ×2, `move_caret_to_end`, `paste_html`) now use Playwright's
  `ControlOrMeta` alias, which resolves to Cmd on macOS and Ctrl on
  Windows/Linux. Available since Playwright 1.40 (already our declared min).
- **Windows / Linux Find & Replace.** Google Docs binds Cmd+Shift+H on macOS
  but Ctrl+H (no Shift) on Windows/Linux. `tools._find_replace_shortcut()`
  branches by `platform.system()` since this is a Shift-component swap, not
  just a modifier swap, so `ControlOrMeta` alone doesn't help.
- **Misleading self-test diagnostic.** When the paste silently no-opped, the
  previous error message blamed "canvas read-back limitation" — sending the
  user to look at the screenshot for text that was never written. The report
  now reads back the Doc body length, sets `paste_landed: bool` to
  distinguish (a) "Doc is empty, paste failed" from (b) "Doc has content but
  read-back didn't return the marker slice", and surfaces a specific
  `next_human_action` for each case. New fields: `doc_body_chars`,
  `paste_landed`.

### Added

- 17 new tests including a static grep regression that fails CI if anyone
  reintroduces a bare `keyboard.press("Meta+...")` outside the platform-aware
  `_find_replace_shortcut()` helper. Total: 106.

### Notes

- This is a **must-upgrade** for Windows / Linux users. v0.3.2 and earlier
  have zero functionality on those platforms.
- macOS users keep the same behavior — `ControlOrMeta` resolves to `Meta` on
  Darwin.

## [0.3.2] - 2026-05-31

Verification audit (gpt-5.3-codex) on the v0.3.0 → v0.3.1 diff confirmed all
v0.3.0 CRITICAL/HIGH findings are closed and no new CRITICAL/HIGH was
introduced. Three "partial" closures called out by the verifier are addressed
here.

### Fixed

- **Atomic write tmp filename collision.** v0.3.1's `atomic_write_text` used
  a deterministic `<target>.tmp` name. Two concurrent writers to the same
  baseline file could clobber each other's temp file mid-flight. v0.3.2 uses
  `tempfile.mkstemp(prefix=".<name>.", suffix=".tmp", dir=parent)` so every
  writer gets its own randomized tmp basename. Cleanup is in a `try/except`
  that unlinks the tmp on failure so a crashed write doesn't leak.
- **`docs_check_drift` undeclared focus side effect.** v0.3.1 surfaced
  `clipboard_overwritten` + `selection_changed` but not the fact that
  `page.bring_to_front()` can steal focus from the user's foreground
  window. Response now also includes `tab_focus_changed: true`, and the
  docstring lists all three side effects explicitly.

### Documented

- **TOCTOU residual in `docs_replace_all`.** The pre-clear recapture closes
  the snapshot-to-recapture window but a sub-millisecond keystroke window
  remains between recapture and `Cmd+A + Backspace` landing. Closing it
  would require locking the user out of the tab. The docstring now states
  this residual explicitly; the persistent backup + baseline remain the
  recovery path.

### Acknowledged

- `_short_diff` exposes up to 40 diff lines of Doc content in
  `toctou_summary`. Same class of content exposure as `drift_summary` in
  v0.3.0; verifier rated it informational, not a severity escalation.

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
