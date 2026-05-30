"""Idempotent installer that wires livedocs-bridge into an MCP client.

Goals:
- LLM-friendly: every step emits a structured JSON event, terminal lines
  also human-readable.
- Idempotent: re-running mutates nothing it doesn't have to.
- Atomic: config JSON is patched via temp file + replace, with a timestamped
  backup of the prior file kept beside it.
- Honest: failure modes return a non-zero exit code AND a `next_human_action`
  string the LLM can read aloud.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import __version__, platform_utils as pu

SUPPORTED_CLIENTS = ("claude-desktop", "cursor", "none")


@dataclass
class StepResult:
    step: str
    status: str  # 'ok' | 'skip' | 'fail' | 'action_required'
    detail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out = {"step": self.step, "status": self.status, "detail": self.detail}
        out.update(self.extra)
        return out


@dataclass
class InstallReport:
    version: str
    client: str
    cdp_url: str
    profile_dir: str
    steps: list[StepResult] = field(default_factory=list)
    next_human_action: Optional[str] = None
    success: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": "livedocs-bridge",
            "version": self.version,
            "command": "install",
            "client": self.client,
            "cdp_url": self.cdp_url,
            "profile_dir": self.profile_dir,
            "steps": [s.to_dict() for s in self.steps],
            "success": self.success,
            "next_human_action": self.next_human_action,
        }


def run_install(
    *,
    client: str = "claude-desktop",
    cdp_port: Optional[int] = None,
    profile_dir: Optional[Path] = None,
    launch_chrome: bool = True,
    install_playwright: bool = True,
    json_output: bool = True,
) -> InstallReport:
    """Drive the install sequence end to end. See module docstring for contract."""
    if client not in SUPPORTED_CLIENTS:
        raise ValueError(
            f"unsupported client {client!r}; expected one of {SUPPORTED_CLIENTS}"
        )

    info = pu.collect_platform_info()
    resolved_port = cdp_port if cdp_port is not None else pu.find_free_port()
    resolved_profile = (profile_dir or info.default_profile_dir).expanduser()
    cdp_url = f"http://127.0.0.1:{resolved_port}"

    report = InstallReport(
        version=__version__,
        client=client,
        cdp_url=cdp_url,
        profile_dir=str(resolved_profile),
    )

    # 1. Sanity check python env.
    report.steps.append(
        StepResult(
            step="python_env",
            status="ok",
            detail=f"Python {sys.version.split()[0]} ({sys.executable})",
        )
    )

    # 2. Verify livedocs-bridge entry point is on PATH (or at least resolvable).
    exe = info.livedocs_executable
    if exe is None:
        report.steps.append(
            StepResult(
                step="livedocs_bridge_on_path",
                status="fail",
                detail=(
                    "Could not locate the `livedocs-bridge` console script. "
                    "Re-install with `uvx livedocs-bridge install` or "
                    "`pip install livedocs-bridge` and ensure pip's scripts dir is on PATH."
                ),
            )
        )
        report.success = False
        report.next_human_action = (
            "Re-install the package so the `livedocs-bridge` command is on PATH."
        )
        return _emit(report, json_output)
    report.steps.append(
        StepResult(
            step="livedocs_bridge_on_path",
            status="ok",
            detail=f"Found {exe}",
            extra={"executable": str(exe)},
        )
    )

    # 3. Ensure Playwright chromium is present (optional — CDP attach mode does
    #    not technically require it, but most paths exercise it).
    if install_playwright:
        report.steps.append(_ensure_playwright_chromium())
    else:
        report.steps.append(
            StepResult(
                step="playwright_chromium",
                status="skip",
                detail="--no-playwright passed; skipping chromium check.",
            )
        )

    # 4. Resolve / create the Chrome profile directory.
    try:
        resolved_profile.mkdir(parents=True, exist_ok=True)
        report.steps.append(
            StepResult(
                step="chrome_profile_dir",
                status="ok",
                detail=f"Profile dir ready at {resolved_profile}",
                extra={"path": str(resolved_profile)},
            )
        )
    except OSError as e:
        report.steps.append(
            StepResult(
                step="chrome_profile_dir",
                status="fail",
                detail=f"Could not create profile dir: {e}",
            )
        )
        report.success = False
        report.next_human_action = (
            f"Create the directory {resolved_profile} manually and re-run install."
        )
        return _emit(report, json_output)

    # 5. Locate Chrome binary and optionally launch it.
    if info.chrome_executable is None:
        report.steps.append(
            StepResult(
                step="chrome_locate",
                status="fail",
                detail="Could not locate Google Chrome / Chromium on this machine.",
            )
        )
        report.success = False
        report.next_human_action = (
            "Install Google Chrome (https://www.google.com/chrome/) and re-run install."
        )
    else:
        report.steps.append(
            StepResult(
                step="chrome_locate",
                status="ok",
                detail=f"Chrome at {info.chrome_executable}",
                extra={"chrome": str(info.chrome_executable)},
            )
        )
        if launch_chrome:
            report.steps.append(
                _launch_chrome(
                    info.chrome_executable, resolved_port, resolved_profile
                )
            )
        else:
            report.steps.append(
                StepResult(
                    step="chrome_launch",
                    status="skip",
                    detail="--no-launch passed; Chrome not started.",
                )
            )

    # 6. Patch MCP client config (atomic, backup, idempotent).
    if client == "none":
        report.steps.append(
            StepResult(
                step="client_config",
                status="skip",
                detail="--client=none; no client config touched.",
            )
        )
    else:
        report.steps.append(
            _patch_client_config(
                client=client,
                cdp_url=cdp_url,
                executable=exe,
                info=info,
            )
        )

    # 7. Probe CDP endpoint to surface "Chrome up but not yet listening" cases.
    if pu.cdp_endpoint_alive(cdp_url, timeout=2.0):
        report.steps.append(
            StepResult(
                step="cdp_probe",
                status="ok",
                detail=f"CDP responding at {cdp_url}",
            )
        )
    else:
        report.steps.append(
            StepResult(
                step="cdp_probe",
                status="action_required",
                detail=(
                    f"CDP not yet responding at {cdp_url}. If Chrome was just "
                    "launched, give it a few seconds and re-run "
                    "`livedocs-bridge doctor`."
                ),
            )
        )

    # 8. Determine final next-step instruction.
    report.success = all(s.status in ("ok", "skip", "action_required") for s in report.steps)
    if any(s.status == "fail" for s in report.steps):
        report.success = False

    if report.success and client == "claude-desktop":
        report.next_human_action = (
            "1) Log in to Google in the Chrome window that just opened, "
            "2) Cmd+Q (fully quit) Claude Desktop and re-open it, "
            "3) Run `livedocs-bridge self-test` to verify, "
            "4) Ask your agent to edit a Doc."
        )
    elif report.success and client == "cursor":
        report.next_human_action = (
            "1) Log in to Google in the Chrome window that just opened, "
            "2) Reload Cursor (Cmd+Shift+P → Developer: Reload Window), "
            "3) Run `livedocs-bridge self-test` to verify."
        )
    elif report.success and client == "none":
        report.next_human_action = (
            "Install finished. Wire MCP config into your client manually using "
            "the `mcp_config_snippet` printed below."
        )
        report.steps.append(
            StepResult(
                step="mcp_config_snippet",
                status="ok",
                detail="Snippet for manual wiring.",
                extra={
                    "snippet": {
                        "mcpServers": {
                            "livedocs-bridge": _server_entry(exe, cdp_url)
                        }
                    }
                },
            )
        )

    return _emit(report, json_output)


def _emit(report: InstallReport, json_output: bool) -> InstallReport:
    payload = report.to_dict()
    if json_output:
        print(json.dumps(payload, indent=2))
    else:
        _print_human(payload)
    return report


def _print_human(payload: dict[str, Any]) -> None:
    status_glyph = {"ok": "✓", "skip": "—", "fail": "✗", "action_required": "!"}
    print(f"livedocs-bridge install (v{payload['version']}) -> {payload['client']}")
    for s in payload["steps"]:
        glyph = status_glyph.get(s["status"], "?")
        print(f"  {glyph} {s['step']}: {s['detail']}")
    if payload["next_human_action"]:
        print()
        print("Next:", payload["next_human_action"])


def _ensure_playwright_chromium() -> StepResult:
    """Try `python -m playwright install chromium` if the browser dir is empty."""
    cache_root = _playwright_cache_dir()
    if _has_chromium(cache_root):
        return StepResult(
            step="playwright_chromium",
            status="ok",
            detail=f"Chromium present in {cache_root}",
        )
    cmd = [sys.executable, "-m", "playwright", "install", "chromium"]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=600
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return StepResult(
            step="playwright_chromium",
            status="fail",
            detail=f"Could not run `{' '.join(cmd)}`: {e}",
        )
    if proc.returncode != 0:
        return StepResult(
            step="playwright_chromium",
            status="fail",
            detail=(
                f"`playwright install chromium` exited {proc.returncode}: "
                f"{(proc.stderr or proc.stdout or '').strip()[:500]}"
            ),
        )
    return StepResult(
        step="playwright_chromium",
        status="ok",
        detail="Installed Playwright chromium.",
    )


def _playwright_cache_dir() -> Path:
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env:
        return Path(env)
    system = pu.detect_system()
    if system == "macos":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    if system == "windows":
        local = os.environ.get("LOCALAPPDATA")
        return (
            Path(local) / "ms-playwright"
            if local
            else Path.home() / "AppData" / "Local" / "ms-playwright"
        )
    return Path.home() / ".cache" / "ms-playwright"


def _has_chromium(root: Path) -> bool:
    if not root.exists():
        return False
    return any(child.name.startswith("chromium") for child in root.iterdir())


def _launch_chrome(
    chrome: Path, port: int, profile: Path
) -> StepResult:
    """Spawn Chrome detached. Returns immediately; CDP readiness is probed later."""
    if pu.cdp_endpoint_alive(f"http://127.0.0.1:{port}", timeout=0.5):
        return StepResult(
            step="chrome_launch",
            status="ok",
            detail=f"CDP already responding on :{port}; skipping launch.",
            extra={"port": port, "reused": True},
        )
    args = [
        str(chrome),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    creationflags = 0
    kwargs: dict[str, Any] = {}
    if pu.detect_system() == "windows":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — survive parent exit.
        creationflags = 0x00000008 | 0x00000200
        kwargs["creationflags"] = creationflags
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=(pu.detect_system() != "windows"),
            **kwargs,
        )
    except OSError as e:
        return StepResult(
            step="chrome_launch",
            status="fail",
            detail=f"Could not start Chrome: {e}",
        )

    # Poll up to 8s for CDP to come up.
    deadline = time.time() + 8.0
    while time.time() < deadline:
        if pu.cdp_endpoint_alive(f"http://127.0.0.1:{port}", timeout=0.3):
            return StepResult(
                step="chrome_launch",
                status="ok",
                detail=f"Chrome PID {proc.pid} on port {port}",
                extra={"pid": proc.pid, "port": port},
            )
        time.sleep(0.4)
    return StepResult(
        step="chrome_launch",
        status="action_required",
        detail=(
            f"Chrome PID {proc.pid} launched but CDP not yet responding on "
            f":{port}. Give it a few seconds and run `livedocs-bridge doctor`."
        ),
        extra={"pid": proc.pid, "port": port},
    )


def _server_entry(exe: Path, cdp_url: str) -> dict[str, Any]:
    return {
        "command": str(exe),
        "args": ["serve"],
        "env": {"LIVEDOCS_CDP_URL": cdp_url},
    }


def _patch_client_config(
    *,
    client: str,
    cdp_url: str,
    executable: Path,
    info: pu.PlatformInfo,
) -> StepResult:
    target = (
        info.claude_desktop_config if client == "claude-desktop" else info.cursor_config
    )
    target.parent.mkdir(parents=True, exist_ok=True)

    existing: dict[str, Any] = {}
    if target.exists():
        try:
            existing = json.loads(target.read_text(encoding="utf-8") or "{}")
            if not isinstance(existing, dict):
                existing = {}
        except json.JSONDecodeError as e:
            backup = _timestamped_backup(target)
            return StepResult(
                step="client_config",
                status="fail",
                detail=(
                    f"Existing config at {target} is not valid JSON ({e}). "
                    f"Original backed up to {backup}; fix or delete it then re-run."
                ),
                extra={"path": str(target), "backup": str(backup)},
            )

    servers = existing.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        existing["mcpServers"] = servers

    new_entry = _server_entry(executable, cdp_url)
    prior = servers.get("livedocs-bridge")
    if prior == new_entry:
        return StepResult(
            step="client_config",
            status="ok",
            detail=f"Config at {target} already has matching livedocs-bridge entry.",
            extra={"path": str(target), "changed": False},
        )

    backup_path = _timestamped_backup(target) if target.exists() else None
    servers["livedocs-bridge"] = new_entry
    _atomic_write_json(target, existing)
    return StepResult(
        step="client_config",
        status="ok",
        detail=(
            f"Patched {target} (backup: {backup_path or 'n/a'}). "
            f"Restart {client} to pick up the change."
        ),
        extra={
            "path": str(target),
            "backup": str(backup_path) if backup_path else None,
            "changed": True,
        },
    )


def _timestamped_backup(path: Path) -> Path:
    ts = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_suffix(path.suffix + f".bak.{ts}")
    shutil.copy2(path, backup)
    return backup


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
