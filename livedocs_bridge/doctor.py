"""Health check command — structured JSON so LLMs branch on it deterministically.

Every check returns:
  {"ok": bool, "detail": str, "fix": Optional[str]}

`fix` is the single concrete next action a human (or LLM) can take. We never
return a long debugging blob — if a check fails, the `fix` field is what we
expect the caller to execute.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import __version__, platform_utils as pu


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fix: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "detail": self.detail, "fix": self.fix}


@dataclass
class DoctorReport:
    version: str
    cdp_url: str
    checks: dict[str, Check] = field(default_factory=dict)

    @property
    def overall(self) -> str:
        if all(c.ok for c in self.checks.values()):
            return "healthy"
        return "needs_action"

    @property
    def next_human_action(self) -> Optional[str]:
        for c in self.checks.values():
            if not c.ok and c.fix:
                return c.fix
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": "livedocs-bridge",
            "version": self.version,
            "command": "doctor",
            "cdp_url": self.cdp_url,
            "checks": {name: c.to_dict() for name, c in self.checks.items()},
            "overall": self.overall,
            "next_human_action": self.next_human_action,
        }


def run_doctor(
    *,
    cdp_url: Optional[str] = None,
    client: str = "claude-desktop",
    json_output: bool = True,
) -> DoctorReport:
    info = pu.collect_platform_info()
    resolved_cdp = cdp_url or os.environ.get(
        "LIVEDOCS_CDP_URL", f"http://127.0.0.1:{pu.DEFAULT_CDP_PORT}"
    )
    report = DoctorReport(version=__version__, cdp_url=resolved_cdp)

    # 1. Are we even installed as a script?
    if info.livedocs_executable:
        report.checks["livedocs_bridge_on_path"] = Check(
            name="livedocs_bridge_on_path",
            ok=True,
            detail=f"Found {info.livedocs_executable}",
        )
    else:
        report.checks["livedocs_bridge_on_path"] = Check(
            name="livedocs_bridge_on_path",
            ok=False,
            detail="`livedocs-bridge` not on PATH.",
            fix="Re-install: `uvx livedocs-bridge install` or `pip install livedocs-bridge`.",
        )

    # 2. Chrome binary present?
    if info.chrome_executable:
        report.checks["chrome_installed"] = Check(
            name="chrome_installed",
            ok=True,
            detail=f"Chrome at {info.chrome_executable}",
        )
    else:
        report.checks["chrome_installed"] = Check(
            name="chrome_installed",
            ok=False,
            detail="Could not find Google Chrome / Chromium.",
            fix="Install Chrome from https://www.google.com/chrome/.",
        )

    # 3. CDP endpoint reachable?
    if pu.cdp_endpoint_alive(resolved_cdp):
        report.checks["chrome_cdp_reachable"] = Check(
            name="chrome_cdp_reachable",
            ok=True,
            detail=f"CDP responding at {resolved_cdp}",
        )
    else:
        report.checks["chrome_cdp_reachable"] = Check(
            name="chrome_cdp_reachable",
            ok=False,
            detail=f"No response from {resolved_cdp}/json/version",
            fix=(
                "Start Chrome with the CDP profile: "
                "`livedocs-bridge launch-chrome`."
            ),
        )

    # 4. MCP client config wired?
    config_path = (
        info.claude_desktop_config if client == "claude-desktop" else info.cursor_config
    )
    report.checks["client_config_has_entry"] = _check_client_config(
        client=client, path=config_path, expected_cdp=resolved_cdp
    )

    # 5. Default profile dir exists / writable?
    profile_dir = info.default_profile_dir
    report.checks["chrome_profile_dir"] = _check_profile_dir(profile_dir)

    if json_output:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        _print_human(report.to_dict())
    return report


def _check_client_config(
    *, client: str, path: Path, expected_cdp: str
) -> Check:
    if not path.exists():
        return Check(
            name="client_config_has_entry",
            ok=False,
            detail=f"{client} config not found at {path}",
            fix=(
                f"Run `livedocs-bridge install --client={client}` to create it."
            ),
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError as e:
        return Check(
            name="client_config_has_entry",
            ok=False,
            detail=f"{client} config at {path} is not valid JSON: {e}",
            fix=f"Open {path}, fix the JSON syntax, then run `livedocs-bridge install --client={client}` again.",
        )
    entry = (data.get("mcpServers") or {}).get("livedocs-bridge")
    if not entry:
        return Check(
            name="client_config_has_entry",
            ok=False,
            detail=f"No livedocs-bridge entry in {path}",
            fix=f"Run `livedocs-bridge install --client={client}` to add it.",
        )
    env = entry.get("env") or {}
    actual_cdp = env.get("LIVEDOCS_CDP_URL")
    if actual_cdp and actual_cdp != expected_cdp:
        return Check(
            name="client_config_has_entry",
            ok=False,
            detail=(
                f"{client} config points at {actual_cdp} but doctor was asked "
                f"to probe {expected_cdp}."
            ),
            fix=(
                f"Either pass `--cdp-url {actual_cdp}` to doctor, or re-run "
                f"`livedocs-bridge install --client={client} --cdp-port <new>`."
            ),
        )
    return Check(
        name="client_config_has_entry",
        ok=True,
        detail=f"livedocs-bridge wired into {path}",
    )


def _check_profile_dir(profile_dir: Path) -> Check:
    if profile_dir.exists() and os.access(profile_dir, os.W_OK):
        return Check(
            name="chrome_profile_dir",
            ok=True,
            detail=f"Profile dir writable at {profile_dir}",
        )
    if profile_dir.exists():
        return Check(
            name="chrome_profile_dir",
            ok=False,
            detail=f"Profile dir {profile_dir} is not writable.",
            fix=f"chmod +w {profile_dir} or pick a different `--profile-dir`.",
        )
    return Check(
        name="chrome_profile_dir",
        ok=False,
        detail=f"Profile dir {profile_dir} does not exist yet.",
        fix="Run `livedocs-bridge install` to create it.",
    )


def _print_human(payload: dict[str, Any]) -> None:
    print(f"livedocs-bridge doctor (v{payload['version']}) — overall: {payload['overall']}")
    for name, c in payload["checks"].items():
        glyph = "✓" if c["ok"] else "✗"
        print(f"  {glyph} {name}: {c['detail']}")
        if not c["ok"] and c.get("fix"):
            print(f"      fix: {c['fix']}")
    if payload["next_human_action"]:
        print()
        print("Next:", payload["next_human_action"])
