"""Unit tests for platform_utils."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from livedocs_bridge import platform_utils as pu


def test_detect_system_returns_known_value():
    assert pu.detect_system() in {"macos", "linux", "windows"}


def test_claude_desktop_config_path_is_absolute():
    p = pu.claude_desktop_config_path()
    assert isinstance(p, Path)
    assert p.is_absolute()
    assert p.name == "claude_desktop_config.json"


def test_cursor_config_path_is_absolute():
    p = pu.cursor_config_path()
    assert p.is_absolute()
    assert p.name == "mcp.json"


def test_default_profile_dir_is_in_home():
    p = pu.default_profile_dir()
    assert str(p).startswith(str(Path.home()))


def test_find_free_port_returns_unbound_port():
    port = pu.find_free_port(preferred=53921)
    # Either preferred is free (port == 53921) or we got an offset within range.
    assert isinstance(port, int) and 1024 <= port <= 65535
    # Confirm we can actually bind to it.
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))


def test_find_free_port_skips_busy(monkeypatch):
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        busy_port = held.getsockname()[1]
        port = pu.find_free_port(preferred=busy_port)
        assert port != busy_port


def test_port_is_free_true_for_random():
    # Find a port we know is free by binding+releasing.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
    # After close, it should be free (modulo TIME_WAIT race; retry once if needed).
    assert pu.port_is_free(p) or pu.port_is_free(p)


def test_cdp_endpoint_alive_false_for_garbage():
    assert pu.cdp_endpoint_alive("http://127.0.0.1:1", timeout=0.3) is False


def test_collect_platform_info_returns_dataclass():
    info = pu.collect_platform_info()
    assert info.system in {"macos", "linux", "windows"}
    assert isinstance(info.claude_desktop_config, Path)
    assert isinstance(info.default_profile_dir, Path)
