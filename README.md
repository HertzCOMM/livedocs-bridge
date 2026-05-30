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
| `docs_replace_all` | `(content: str, content_type: 'markdown' \| 'html' = 'markdown')` | Wipe the Doc and inject new content. |
| `docs_append` | `(content: str, content_type = 'markdown')` | Append to the end of the Doc. |
| `docs_find_replace` | `(find: str, replace: str, all_occurrences: bool = True)` | Replace via the Docs Find & Replace dialog. |
| `docs_screenshot` | `(scroll_to: 'top' \| 'bottom' \| 'current' = 'top', path: str \| None = None)` | Capture viewport as PNG (path or base64). |
| `docs_get_state` | `()` | Doc URL, title, char count, observed-at timestamp. |

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

## Environment variables

| Name | Default | Notes |
| --- | --- | --- |
| `LIVEDOCS_CDP_URL` | `http://127.0.0.1:19825` | Chrome DevTools Protocol endpoint. |
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

## Limits & risks

- **DOM brittleness.** We depend on the class name `docs-texteventtarget-iframe`
  and `.kix-appview-editor`. If Google changes them, we update a selector.
- **Single-tab routing.** v0.1 talks to the first matching Doc tab. Multi-tab
  fan-out is a v0.2 feature.
- **ToS gray area.** Browser automation against Google Docs is not explicitly
  banned, but at very high frequencies it can trip anti-abuse heuristics. This
  is built for human-in-the-loop agent editing, not bulk farming.
- **Chrome dependency.** Must be running with `--remote-debugging-port` and
  logged in. The server doesn't launch or manage Chrome for you.

---

## Roadmap

- **v0.1** (now) — six core tools, Google Docs.
- **v0.2** — multi-tab routing; Google Sheets and Slides (same canvas + iframe
  architecture, different selectors).
- **v0.3** — Notion and Coda (different DOM, same CDP attach pattern).

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
