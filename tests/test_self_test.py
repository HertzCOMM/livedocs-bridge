"""Tests for the self-test command's pre-flight + emit shape.

We don't drive a real browser. We test the no-CDP shortcut path and the
report serialization.
"""

from __future__ import annotations

import json

import pytest

from livedocs_bridge import self_test as st


def test_self_test_returns_error_when_cdp_dead(monkeypatch, capsys):
    monkeypatch.setattr(st.pu, "cdp_endpoint_alive", lambda url, timeout=2.0: False)
    report = st.run_self_test(cdp_url="http://127.0.0.1:1", json_output=True)
    assert report.success is False
    assert "CDP not reachable" in (report.error or "")
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is False


def test_self_test_report_round_trips(monkeypatch, capsys):
    monkeypatch.setattr(st.pu, "cdp_endpoint_alive", lambda url, timeout=2.0: False)
    report = st.run_self_test(json_output=True)
    payload = json.loads(capsys.readouterr().out)
    assert payload["tool"] == "livedocs-bridge"
    assert payload["command"] == "self-test"
    assert "cdp_url" in payload
    assert "elapsed_seconds" in payload
    assert isinstance(payload["elapsed_seconds"], (int, float))


def test_marker_template_includes_version_and_timestamp():
    msg = st.MARKER_TEMPLATE.format(version="9.9.9", ts="2099-12-31 23:59:59")
    assert "9.9.9" in msg
    assert "2099-12-31" in msg
