"""v0.3.4 — Bug 3 regression: CDP connect timeout.

After ~2-3 days of Chrome uptime, `connect_over_cdp` hangs at the protocol
handshake even though `/json/version` still returns 200. Playwright default
is 180s; v0.3.4 cuts to 30s and raises `CDPConnectTimeout` with a Chrome-
restart message instead of letting callers wait the full default.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeout

from livedocs_bridge import playwright_core as core


def test_default_timeout_constant():
    assert core.DEFAULT_CDP_CONNECT_TIMEOUT_MS == 30000


def test_timeout_env_override(monkeypatch):
    monkeypatch.setenv("LIVEDOCS_CDP_CONNECT_TIMEOUT_MS", "5000")
    assert core.get_cdp_connect_timeout_ms() == 5000


def test_timeout_env_garbage_falls_back(monkeypatch):
    monkeypatch.setenv("LIVEDOCS_CDP_CONNECT_TIMEOUT_MS", "not-an-int")
    assert core.get_cdp_connect_timeout_ms() == core.DEFAULT_CDP_CONNECT_TIMEOUT_MS


def test_timeout_default_when_env_absent(monkeypatch):
    monkeypatch.delenv("LIVEDOCS_CDP_CONNECT_TIMEOUT_MS", raising=False)
    assert core.get_cdp_connect_timeout_ms() == core.DEFAULT_CDP_CONNECT_TIMEOUT_MS


def test_cdp_connect_timeout_is_runtime_error_subclass():
    assert issubclass(core.CDPConnectTimeout, RuntimeError)


async def test_browser_session_raises_specific_error_on_handshake_timeout(monkeypatch):
    # Simulate the production failure mode: HTTP probe is fine, CDP handshake
    # hangs. `connect_over_cdp` raises PlaywrightTimeout; we want a specific
    # `CDPConnectTimeout` with Chrome-restart instructions.
    fake_pw = MagicMock()
    fake_pw.stop = AsyncMock()
    fake_pw.chromium = MagicMock()
    fake_pw.chromium.connect_over_cdp = AsyncMock(
        side_effect=PlaywrightTimeout("Timeout 30000ms exceeded")
    )

    async def fake_start():
        return fake_pw

    monkeypatch.setattr(core, "async_playwright", lambda: MagicMock(start=fake_start))

    session = core.BrowserSession(cdp_url="http://127.0.0.1:19825")
    with pytest.raises(core.CDPConnectTimeout) as excinfo:
        await session.start()
    msg = str(excinfo.value)
    assert "connect_over_cdp" in msg
    assert "Chrome" in msg
    assert "launch-chrome" in msg
    assert "user-data-dir" in msg
    # The half-started playwright instance must be torn down so the next
    # attempt doesn't inherit corrupted state.
    fake_pw.stop.assert_awaited_once()
    assert session._pw is None


# v0.3.5 — codex audit regressions on the v0.3.4 CDP timeout patch.

def test_timeout_env_negative_falls_back_to_default(monkeypatch):
    # LOW #6: negative env value was previously passed straight to Playwright.
    monkeypatch.setenv("LIVEDOCS_CDP_CONNECT_TIMEOUT_MS", "-5000")
    assert core.get_cdp_connect_timeout_ms() == core.DEFAULT_CDP_CONNECT_TIMEOUT_MS


def test_timeout_env_below_floor_falls_back(monkeypatch):
    monkeypatch.setenv("LIVEDOCS_CDP_CONNECT_TIMEOUT_MS", "500")
    # 500ms is sub-second — too short for a real CDP session. We snap back to
    # the default rather than honor it.
    assert core.get_cdp_connect_timeout_ms() == core.DEFAULT_CDP_CONNECT_TIMEOUT_MS


def test_timeout_env_at_floor_accepted(monkeypatch):
    monkeypatch.setenv("LIVEDOCS_CDP_CONNECT_TIMEOUT_MS", "1000")
    assert core.get_cdp_connect_timeout_ms() == 1000


def test_min_timeout_constant_exposed():
    assert core.MIN_CDP_CONNECT_TIMEOUT_MS == 1000


async def test_timeout_error_message_is_generic_then_specific(monkeypatch):
    # LOW #5: recovery message must work for bb-browser / daemon / remote
    # setups, not just users who launched Chrome via livedocs-bridge.
    fake_pw = MagicMock()
    fake_pw.stop = AsyncMock()
    fake_pw.chromium = MagicMock()
    fake_pw.chromium.connect_over_cdp = AsyncMock(
        side_effect=PlaywrightTimeout("Timeout 30000ms exceeded")
    )

    async def fake_start():
        return fake_pw

    monkeypatch.setattr(core, "async_playwright", lambda: MagicMock(start=fake_start))

    session = core.BrowserSession(cdp_url="http://127.0.0.1:19825")
    with pytest.raises(core.CDPConnectTimeout) as excinfo:
        await session.start()
    msg = str(excinfo.value)
    # Generic recovery FIRST.
    assert "restart the chrome instance" in msg.lower()
    assert "--remote-debugging-port" in msg
    # livedocs-bridge mention demoted to "If livedocs-bridge owns".
    assert "if livedocs-bridge owns" in msg.lower()


async def test_browser_session_passes_timeout_kwarg(monkeypatch):
    captured: dict = {}

    async def capture_connect(url, **kwargs):
        captured["url"] = url
        captured["timeout"] = kwargs.get("timeout")
        # Return a sentinel browser.
        return MagicMock(name="browser")

    fake_pw = MagicMock()
    fake_pw.stop = AsyncMock()
    fake_pw.chromium = MagicMock()
    fake_pw.chromium.connect_over_cdp = capture_connect

    async def fake_start():
        return fake_pw

    monkeypatch.setattr(core, "async_playwright", lambda: MagicMock(start=fake_start))
    monkeypatch.setenv("LIVEDOCS_CDP_CONNECT_TIMEOUT_MS", "12345")

    session = core.BrowserSession(cdp_url="http://127.0.0.1:19825")
    await session.start()
    assert captured["timeout"] == 12345
    assert captured["url"] == "http://127.0.0.1:19825"
