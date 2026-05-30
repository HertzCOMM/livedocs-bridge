# livedocs-bridge

> **Live edit Google Docs from your LLM agent. No OAuth. No SaaS. Just CDP.**

Any MCP-compatible LLM client — Claude Desktop, Cursor, Cline, Continue, Windsurf — can
now edit the Google Doc that's already open in your Chrome. Same URL, same login,
nothing leaves your machine.

![hero — HTML paste renders as native Docs formatting](docs/screenshots/03_html_paste_native_formatted.png)

---

## Why

The existing options all force a trade-off. `livedocs-bridge` is the only one
that fills all four columns:

| Approach | Edits the same Doc? | Agent-driven? | No OAuth? | Fully local? |
| --- | :---: | :---: | :---: | :---: |
| Google Docs API `batchUpdate` | ✅ | ✅ | ❌ | ❌ |
| Glasp / Docs AI sidebar | ✅ | ❌ (chat only) | ✅ | ❌ |
| OAuth-based Google Docs MCP servers | ✅ | ✅ | ❌ | ❌ |
| Apps Script | ✅ (in-doc) | ❌ | ✅ | ✅ |
| Zapier / Make | ❌ (creates new) | ❌ | ❌ | ❌ |
| **livedocs-bridge** | **✅** | **✅** | **✅** | **✅** |

The wedge: *your* agent + *your* browser + *your* Doc, with nothing in the middle.

---

## How it works

```
LLM client (Claude Desktop / Cursor / Cline / ...)
    │  MCP stdio
    ▼
livedocs-bridge  ──── Playwright async API ────►  Chrome (CDP port 19825)
                                                       │
                                                       ▼
                                              Google Docs tab (already logged in)
```

We attach to your existing Chrome over the Chrome DevTools Protocol, dive into the
nested `iframe.docs-texteventtarget-iframe` that Docs actually listens to, and drive
the editor with frame-scoped keyboard / clipboard. No data leaves the machine.

---

## Install in one command

```bash
uvx livedocs-bridge install
```

or, if you prefer pip:

```bash
pip install livedocs-bridge && livedocs-bridge install
```

That single command does everything:

- Ensures Playwright chromium is present
- Finds a free CDP port
- Locates Chrome / Chromium on your OS
- Launches Chrome detached with a dedicated profile (`~/.livedocs-chrome-profile`)
- Atomically patches your `claude_desktop_config.json` (timestamped backup kept)
- Probes the CDP endpoint to confirm it's actually responding
- Prints a JSON report and the single concrete next thing you have to do

```bash
livedocs-bridge install --client=cursor      # Cursor instead
livedocs-bridge install --client=none        # just launch Chrome, give me the JSON snippet
livedocs-bridge install --no-launch          # patch config but don't start Chrome
livedocs-bridge install --no-json            # human-readable output
```

After install, **two things you still have to do yourself** (LLMs can't):

1. Log in to Google in the new Chrome window.
2. Cmd+Q quit Claude Desktop and re-open it.

Then verify:

```bash
livedocs-bridge self-test
```

This opens a throwaway Doc, edits it, screenshots, and reads the marker back
to confirm the install actually works. If you see `"success": true` and the
screenshot shows the marker line, you're done.

---

## Letting an LLM install it for you

Any LLM client that can run shell commands (Claude Code, Cursor agent, Cline,
Continue, Windsurf) can install this end-to-end. Paste this:

> Install `https://github.com/HertzCOMM/livedocs-bridge` and wire it into my
> Claude Desktop. Use `uvx livedocs-bridge install` then run
> `livedocs-bridge doctor` to confirm.

Your only manual steps are: (1) approve the bash commands, (2) log in to Google
in the Chrome window that opens, (3) Cmd+Q Claude Desktop and reopen it.
Everything else the agent does for you.

---

## Setup Chrome manually (only if you skip `install`)

```bash
# macOS
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=19825 \
  --user-data-dir="$HOME/.livedocs-chrome-profile"
```

```bash
# Linux
google-chrome \
  --remote-debugging-port=19825 \
  --user-data-dir="$HOME/.livedocs-chrome-profile"
```

```powershell
# Windows
& "C:\Program Files\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=19825 `
  --user-data-dir="$env:USERPROFILE\.livedocs-chrome-profile"
```

Or use the bundled helper, which picks the right Chrome binary and port for you:

```bash
livedocs-bridge launch-chrome
```

---

## Wire into your MCP client manually (only if you skip `install`)

### Claude Desktop

`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or
`%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "livedocs-bridge": {
      "command": "livedocs-bridge",
      "args": ["serve"],
      "env": { "LIVEDOCS_CDP_URL": "http://127.0.0.1:19825" }
    }
  }
}
```

### Cursor

`~/.cursor/mcp.json` — same block as above.

### Cline (VS Code)

`Cline > MCP Servers > Edit JSON`:

```json
{
  "mcpServers": {
    "livedocs-bridge": {
      "command": "livedocs-bridge",
      "args": ["serve"],
      "env": { "LIVEDOCS_CDP_URL": "http://127.0.0.1:19825" },
      "autoApprove": ["docs_get_state", "docs_screenshot"]
    }
  }
}
```

Continue and Windsurf use the same MCP-stdio config shape — drop the same block
into their config file.

Full examples live in [`examples/`](examples/).

---

## Diagnose when something breaks

```bash
livedocs-bridge doctor
```

prints a JSON report with one check per concern. Each failed check carries a
`fix` field with the exact command to run. Sample output:

```json
{
  "checks": {
    "livedocs_bridge_on_path":  {"ok": true,  "detail": "Found ..."},
    "chrome_installed":         {"ok": true,  "detail": "Chrome at ..."},
    "chrome_cdp_reachable":     {"ok": false, "detail": "No response from http://127.0.0.1:19825",
                                 "fix": "Start Chrome with the CDP profile: `livedocs-bridge launch-chrome`."},
    "client_config_has_entry":  {"ok": true,  "detail": "livedocs-bridge wired into ..."},
    "chrome_profile_dir":       {"ok": true,  "detail": "Profile dir writable at ..."}
  },
  "overall": "needs_action",
  "next_human_action": "Start Chrome with the CDP profile: `livedocs-bridge launch-chrome`."
}
```

Pass `--no-json` for human-readable output.

---

## Use

Open the Doc you want to edit in your CDP-attached Chrome, then in your LLM client:

> Open `https://docs.google.com/document/d/<id>/edit` and replace section 3,
> paragraph 2 with: "... new content ...".

The agent calls `docs_open` → `docs_find_replace`. The Doc updates in place. URL
unchanged. No browser tab popped, no permission prompt, no API key.

---

## MCP tools

| Name | Signature | Purpose |
| --- | --- | --- |
| `docs_open` | `(url: str)` | Open or focus a Doc tab. `docs.new` creates a fresh Doc. |
| `docs_replace_all` | `(content, content_type='markdown', doc_url=None, force=False)` | Wipe + inject. Drift-checked + auto-backup. Pass `doc_url` to pin a specific Doc. `force=True` overrides drift abort. |
| `docs_append` | `(content, content_type='markdown', doc_url=None)` | Append. Snapshots the Doc first. |
| `docs_find_replace` | `(find, replace, all_occurrences=True)` | Replace via the Docs Find & Replace dialog. |
| `docs_screenshot` | `(scroll_to='top', path=None)` | Capture viewport as PNG (path or base64). |
| `docs_get_state` | `()` | Doc URL, title, char count, observed-at timestamp. |
| `docs_check_drift` | `(doc_url=None)` | Preview drift without writing. Returns `{drifted, drift_summary, baseline_exists}`. |
| `docs_restore_from_backup` | `(doc_url=None, backup_timestamp=None)` | Restore the Doc from the latest (or a specific) auto-backup. Snapshots the pre-restore state first. |

All tools return `{"success": bool, ...}` so MCP clients can branch on the result
instead of catching exceptions.

## CLI subcommands

| Command | Purpose |
| --- | --- |
| `livedocs-bridge serve` | Run the MCP stdio server. This is what your MCP client launches. Default when run with no args. |
| `livedocs-bridge install` | Idempotent install + client wiring. JSON output by default. Non-zero exit on failure. |
| `livedocs-bridge doctor` | Structured health check. Exit code `0` healthy / `2` needs action. |
| `livedocs-bridge self-test` | End-to-end smoke that writes a marker to a throwaway Doc and screenshots it. Exit code `0` pass / `3` fail. |
| `livedocs-bridge launch-chrome` | Start the managed CDP Chrome without touching any MCP config. |

Every subcommand prints a single JSON object to stdout. Exit codes are stable
so LLMs can branch deterministically without parsing stderr.

---

## Drift detection + backups

Every `docs_replace_all` and `docs_append` snapshots the Doc to a persistent
backup directory before clearing or appending. `docs_replace_all` additionally
compares the current Doc against the last push baseline; if they don't match,
the user has edited the Doc in the meantime and the call aborts with the diff
unless you pass `force=True`.

```
~/.livedocs-bridge/backups/
├── _last_pushed_<doc_id>.txt              # baseline, never pruned
├── doc_backup_<YYYYMMDD_HHMMSS>_<short>.txt
├── doc_backup_<YYYYMMDD_HHMMSS>_<short>.html
└── ... (anything older than 30d is auto-pruned on next write)
```

Override the location with `LIVEDOCS_BACKUP_DIR=/abs/path`. Recommended
workflow when you're orchestrating writes from an agent:

1. Call `docs_check_drift(doc_url=...)` and show the diff if `drifted` is true.
2. Ask the user whether to proceed.
3. Call `docs_replace_all(..., doc_url=..., force=True)` if approved.
4. If the result looks wrong, call
   `docs_restore_from_backup(doc_url=...)` to roll back to the
   most recent pre-write snapshot.

## Environment variables

| Name | Default | Notes |
| --- | --- | --- |
| `LIVEDOCS_CDP_URL` | `http://127.0.0.1:19825` | Chrome DevTools Protocol endpoint. |
| `LIVEDOCS_BACKUP_DIR` | `~/.livedocs-bridge/backups` | Persistent backup root. |
| `LIVEDOCS_LOG_LEVEL` | `INFO` | Python log level (`DEBUG` for verbose). |
| `LIVEDOCS_LOG_FILE` | unset | If set, also write logs to this file. Stderr is always used. |

---

## Try it without an MCP client

```bash
python examples/quickstart.py
```

This replaces the body of whatever Google Doc is currently focused in your
CDP-attached Chrome with a sample memo. Useful to sanity-check the install.

---

## Production gotchas

These are the failure modes a real 8-hour, 20-iteration workflow surfaced.
Most of them are now handled inside the library; the last one is documented
because no in-band fix is possible.

1. **bb-browser MCP `browser_press` doesn't reach the Docs handler.** Docs
   listens inside the nested `iframe.docs-texteventtarget-iframe`. A
   page-level CDP `Input.dispatchKeyEvent` lands on the top frame and never
   reaches Docs. We use Playwright `iframe.content_frame()` for frame-scoped
   keyboard.
2. **`navigator.clipboard.writeText` needs document focus.** Backgrounded
   Chrome throws `Document is not focused`. We `context.grant_permissions`
   + `page.bring_to_front()` + use `clipboard.write([new ClipboardItem({...})])`
   (not `writeText`) so we can set both `text/html` and `text/plain`.
3. **Paste HTML, not markdown.** Docs does NOT auto-render markdown on paste.
   `## Heading` shows as literal text. We always convert markdown → HTML
   with `markdown.markdown(text, extensions=['tables', 'fenced_code', 'nl2br'])`
   before pasting.
4. **`Cmd+Home` does NOT scroll Docs to top.** Use
   `document.querySelector('.kix-appview-editor').scrollTop = 0` via
   `page.evaluate(...)`.
5. **`networkidle` never settles on Docs.** Docs polls continuously. We use
   `wait_until="domcontentloaded"` for every `goto()` and `reload()`.
6. **Idle Docs tabs lose their keystroke iframe.** If a tab has been idle for
   tens of minutes, `iframe.docs-texteventtarget-iframe` may not be in the
   DOM. We wait 45 s and reload the page once on timeout.
7. **Wholesale replace is destructive.** Every `docs_replace_all` overwrites
   anything the user typed since the last push. We always snapshot first and
   compare against the last push baseline; the call aborts with the diff
   unless `force=True`. See "Drift detection + backups" above.
8. **`markdownify` HTML → markdown round-trip is lossy.** Docs emits inline
   styles (`<span style="font-weight:700">`) that don't survive a markdown
   round-trip. We never auto-pull-then-edit; drift detection compares plain
   text only.
9. **Don't smoke-test against your real Doc.** `livedocs-bridge self-test`
   always opens a fresh `docs.new`. If you write your own smoke, do the same.
10. **Chrome long-uptime can corrupt the browser-wide CDP session.** After
    ~2 days of uptime, `connect_over_cdp` can hang at the protocol handshake
    even though `http://.../json/version` still returns 200. There is no
    in-band fix — kill Chrome and relaunch with
    `livedocs-bridge launch-chrome` (or restart your bb-browser daemon if you
    use one). `user-data-dir` is persistent, so your Google login survives.

## Limits & risks

- **DOM brittleness.** We depend on the class names
  `docs-texteventtarget-iframe` and `.kix-appview-editor`. If Google changes
  them, we update a selector.
- **Single-tab routing.** Pass `doc_url=` to pin a specific Doc; otherwise
  we use the first matching Doc tab.
- **ToS gray area.** Browser automation against Google Docs is not explicitly
  banned, but at very high frequencies it can trip anti-abuse heuristics. This
  is built for human-in-the-loop agent editing, not bulk farming.
- **Chrome dependency.** Must be running with `--remote-debugging-port` and
  logged in. The server doesn't launch or manage Chrome for you (use
  `livedocs-bridge launch-chrome` to spawn a managed instance).

---

## Roadmap

- **v0.1** — six core tools, Google Docs.
- **v0.2** — `install` / `doctor` / `self-test` CLI for LLM-driven setup.
- **v0.3** (now) — drift detection + persistent backups + `doc_url` pinning
  + iframe reload retry. Production hardening from real workflow.
- **v0.4** — Google Sheets and Slides (same canvas + iframe architecture,
  different selectors); multi-tab fan-out.
- **v0.5** — Notion and Coda (different DOM, same CDP attach pattern).

---

## Contributing

Issues and PRs welcome. Run the test suite with:

```bash
pip install -e ".[dev]"
pytest -q
```

The tests are mock-based and don't require Chrome.

---

## License

[MIT](LICENSE) — © 2026 HertzFlow Contributors.

---

## Related reading

- Postmortem on why bb-browser MCP `browser_press` can't reach the Docs
  keystroke handler, and why Playwright CDP attach does:
  [docs/why-cdp-attach.md](docs/why-cdp-attach.md) *(coming with v0.2)*
