"""Tests for the install command's pure logic.

We don't actually launch Chrome or touch the user's real config — every test
points at a tmp path and stubs Chrome launch / playwright install / CDP probe.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from livedocs_bridge import install as install_mod
from livedocs_bridge import platform_utils as pu


@pytest.fixture
def fake_platform(tmp_path, monkeypatch):
    """Pretend Chrome is at tmp_path/chrome, configs live in tmp_path."""
    chrome = tmp_path / "chrome"
    chrome.write_text("#!/bin/sh\nsleep 1\n")
    chrome.chmod(0o755)
    claude_cfg = tmp_path / "Claude" / "claude_desktop_config.json"
    cursor_cfg = tmp_path / ".cursor" / "mcp.json"
    profile_dir = tmp_path / ".livedocs-chrome-profile"
    exe = tmp_path / "livedocs-bridge"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)

    fake_info = pu.PlatformInfo(
        system=pu.detect_system(),
        chrome_executable=chrome,
        claude_desktop_config=claude_cfg,
        cursor_config=cursor_cfg,
        default_profile_dir=profile_dir,
        livedocs_executable=exe,
    )
    monkeypatch.setattr(install_mod.pu, "collect_platform_info", lambda: fake_info)
    monkeypatch.setattr(install_mod.pu, "find_free_port", lambda preferred=19825, max_tries=20: 19825)
    monkeypatch.setattr(install_mod.pu, "cdp_endpoint_alive", lambda url, timeout=2.0: True)
    monkeypatch.setattr(install_mod, "_ensure_playwright_chromium", lambda: install_mod.StepResult("playwright_chromium", "ok", "stubbed"))
    monkeypatch.setattr(install_mod, "_launch_chrome", lambda *a, **k: install_mod.StepResult("chrome_launch", "ok", "stubbed"))
    return fake_info


def test_install_writes_claude_desktop_config(fake_platform, capsys):
    report = install_mod.run_install(
        client="claude-desktop", json_output=True
    )
    assert report.success is True
    cfg = json.loads(fake_platform.claude_desktop_config.read_text())
    assert "livedocs-bridge" in cfg["mcpServers"]
    entry = cfg["mcpServers"]["livedocs-bridge"]
    assert entry["command"] == str(fake_platform.livedocs_executable)
    assert entry["args"] == ["serve"]
    assert entry["env"]["LIVEDOCS_CDP_URL"].startswith("http://127.0.0.1:")
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["success"] is True


def test_install_is_idempotent(fake_platform, capsys):
    install_mod.run_install(client="claude-desktop", json_output=True)
    capsys.readouterr()
    report = install_mod.run_install(client="claude-desktop", json_output=True)
    capsys.readouterr()
    steps = {s.step: s for s in report.steps}
    assert steps["client_config"].extra.get("changed") is False


def test_install_preserves_other_servers(fake_platform, capsys):
    fake_platform.claude_desktop_config.parent.mkdir(parents=True, exist_ok=True)
    fake_platform.claude_desktop_config.write_text(
        json.dumps({"mcpServers": {"other-tool": {"command": "/usr/bin/other"}}})
    )
    install_mod.run_install(client="claude-desktop", json_output=True)
    capsys.readouterr()
    cfg = json.loads(fake_platform.claude_desktop_config.read_text())
    assert "other-tool" in cfg["mcpServers"]
    assert "livedocs-bridge" in cfg["mcpServers"]


def test_install_backs_up_existing_config(fake_platform, capsys):
    fake_platform.claude_desktop_config.parent.mkdir(parents=True, exist_ok=True)
    fake_platform.claude_desktop_config.write_text(
        json.dumps({"mcpServers": {"other": {"command": "x"}}})
    )
    install_mod.run_install(client="claude-desktop", json_output=True)
    capsys.readouterr()
    backups = list(fake_platform.claude_desktop_config.parent.glob("*.bak.*"))
    assert backups, "expected a timestamped backup file"


def test_install_handles_corrupt_existing_config(fake_platform, capsys):
    fake_platform.claude_desktop_config.parent.mkdir(parents=True, exist_ok=True)
    fake_platform.claude_desktop_config.write_text("{ this is not json")
    report = install_mod.run_install(client="claude-desktop", json_output=True)
    capsys.readouterr()
    failed = [s for s in report.steps if s.step == "client_config" and s.status == "fail"]
    assert failed, "expected client_config step to fail on corrupt JSON"
    assert report.success is False


def test_install_with_client_none_emits_snippet(fake_platform, capsys):
    report = install_mod.run_install(client="none", json_output=True)
    capsys.readouterr()
    snippet_steps = [s for s in report.steps if s.step == "mcp_config_snippet"]
    assert snippet_steps
    snippet = snippet_steps[0].extra["snippet"]
    assert "mcpServers" in snippet
    assert "livedocs-bridge" in snippet["mcpServers"]


def test_install_rejects_unknown_client(fake_platform):
    with pytest.raises(ValueError):
        install_mod.run_install(client="bogus", json_output=False)


def test_install_fails_when_livedocs_executable_missing(fake_platform, monkeypatch, capsys):
    broken = pu.PlatformInfo(
        system=fake_platform.system,
        chrome_executable=fake_platform.chrome_executable,
        claude_desktop_config=fake_platform.claude_desktop_config,
        cursor_config=fake_platform.cursor_config,
        default_profile_dir=fake_platform.default_profile_dir,
        livedocs_executable=None,
    )
    monkeypatch.setattr(install_mod.pu, "collect_platform_info", lambda: broken)
    report = install_mod.run_install(client="claude-desktop", json_output=True)
    capsys.readouterr()
    assert report.success is False
    assert "PATH" in (report.next_human_action or "")
