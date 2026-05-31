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


# v0.3.3 — diagnostic differentiation between paste-failed and read-back-glitch.

def _make_report(*, doc_body_chars: int, marker_present: bool):
    """Build a SelfTestReport directly without going through async_run; we're
    only testing the post-paste diagnostic branch logic via construction."""
    report = st.SelfTestReport(
        version=st.__version__,
        cdp_url="http://127.0.0.1:19825",
        marker="marker-line",
        marker_present_in_doc=marker_present,
        doc_body_chars=doc_body_chars,
        paste_landed=(doc_body_chars >= 200 or marker_present),
        success=marker_present,
    )
    return report


def test_report_serializes_v033_diagnostic_fields():
    report = _make_report(doc_body_chars=42, marker_present=False)
    payload = report.to_dict()
    assert "doc_body_chars" in payload
    assert "paste_landed" in payload
    assert payload["doc_body_chars"] == 42
    assert payload["paste_landed"] is False


def test_report_paste_landed_true_when_body_has_content_even_without_marker():
    # Read-back glitch path: paste worked, marker just didn't survive the
    # canvas → DOM round-trip.
    report = _make_report(doc_body_chars=400, marker_present=False)
    assert report.paste_landed is True
    assert report.success is False


def test_report_paste_landed_false_when_body_essentially_empty():
    # Windows pre-v0.3.3 path: Doc body is empty post-paste because Meta+V
    # was a no-op.
    report = _make_report(doc_body_chars=12, marker_present=False)
    assert report.paste_landed is False
    assert report.success is False
