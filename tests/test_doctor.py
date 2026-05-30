"""Tests for the doctor command's structured checks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from livedocs_bridge import doctor as doctor_mod
from livedocs_bridge import platform_utils as pu


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    chrome = tmp_path / "chrome"
    chrome.write_text("#!/bin/sh\n")
    chrome.chmod(0o755)
    exe = tmp_path / "livedocs-bridge"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    profile = tmp_path / "profile"
    profile.mkdir()
    claude_cfg = tmp_path / "Claude" / "claude_desktop_config.json"
    cursor_cfg = tmp_path / ".cursor" / "mcp.json"

    info = pu.PlatformInfo(
        system=pu.detect_system(),
        chrome_executable=chrome,
        claude_desktop_config=claude_cfg,
        cursor_config=cursor_cfg,
        default_profile_dir=profile,
        livedocs_executable=exe,
    )
    monkeypatch.setattr(doctor_mod.pu, "collect_platform_info", lambda: info)
    monkeypatch.setattr(doctor_mod.pu, "cdp_endpoint_alive", lambda url, timeout=1.0: False)
    return info


def test_doctor_reports_missing_config(fake_env, capsys):
    report = doctor_mod.run_doctor(cdp_url="http://127.0.0.1:19825", json_output=True)
    capsys.readouterr()
    c = report.checks["client_config_has_entry"]
    assert c.ok is False
    assert "install" in (c.fix or "")
    assert report.overall == "needs_action"


def test_doctor_reports_cdp_unreachable(fake_env, capsys):
    report = doctor_mod.run_doctor(json_output=True)
    capsys.readouterr()
    assert report.checks["chrome_cdp_reachable"].ok is False
    assert report.next_human_action is not None


def test_doctor_passes_when_config_present_and_cdp_alive(fake_env, monkeypatch, capsys):
    fake_env.claude_desktop_config.parent.mkdir(parents=True, exist_ok=True)
    fake_env.claude_desktop_config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "livedocs-bridge": {
                        "command": str(fake_env.livedocs_executable),
                        "args": ["serve"],
                        "env": {"LIVEDOCS_CDP_URL": "http://127.0.0.1:19825"},
                    }
                }
            }
        )
    )
    monkeypatch.setattr(doctor_mod.pu, "cdp_endpoint_alive", lambda url, timeout=1.0: True)
    report = doctor_mod.run_doctor(cdp_url="http://127.0.0.1:19825", json_output=True)
    capsys.readouterr()
    assert report.overall == "healthy"
    assert all(c.ok for c in report.checks.values())


def test_doctor_flags_cdp_mismatch(fake_env, monkeypatch, capsys):
    fake_env.claude_desktop_config.parent.mkdir(parents=True, exist_ok=True)
    fake_env.claude_desktop_config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "livedocs-bridge": {
                        "command": str(fake_env.livedocs_executable),
                        "args": ["serve"],
                        "env": {"LIVEDOCS_CDP_URL": "http://127.0.0.1:11111"},
                    }
                }
            }
        )
    )
    monkeypatch.setattr(doctor_mod.pu, "cdp_endpoint_alive", lambda url, timeout=1.0: True)
    report = doctor_mod.run_doctor(cdp_url="http://127.0.0.1:22222", json_output=True)
    capsys.readouterr()
    c = report.checks["client_config_has_entry"]
    assert c.ok is False
    assert "11111" in c.detail and "22222" in c.detail


def test_doctor_handles_corrupt_config(fake_env, capsys):
    fake_env.claude_desktop_config.parent.mkdir(parents=True, exist_ok=True)
    fake_env.claude_desktop_config.write_text("{ broken")
    report = doctor_mod.run_doctor(json_output=True)
    capsys.readouterr()
    assert report.checks["client_config_has_entry"].ok is False
    assert "valid JSON" in report.checks["client_config_has_entry"].detail


def test_doctor_json_output_shape(fake_env, capsys):
    doctor_mod.run_doctor(json_output=True)
    payload = json.loads(capsys.readouterr().out)
    assert payload["tool"] == "livedocs-bridge"
    assert payload["command"] == "doctor"
    assert "checks" in payload
    assert payload["overall"] in {"healthy", "needs_action"}
