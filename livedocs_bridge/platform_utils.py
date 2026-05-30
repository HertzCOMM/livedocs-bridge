"""Platform detection + path resolution helpers used by install / doctor / self-test.

Everything here is deterministic and side-effect free (no file writes, no
network) so the install pipeline can dry-run and so tests don't need fixtures.
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.error import URLError
from urllib.request import urlopen

DEFAULT_CDP_PORT = 19825
DEFAULT_PROFILE_DIR_NAME = ".livedocs-chrome-profile"


@dataclass(frozen=True)
class PlatformInfo:
    system: str  # 'macos' | 'linux' | 'windows'
    chrome_executable: Optional[Path]
    claude_desktop_config: Path
    cursor_config: Path
    default_profile_dir: Path
    livedocs_executable: Optional[Path]


def detect_system() -> str:
    s = platform.system().lower()
    if s == "darwin":
        return "macos"
    if s == "windows":
        return "windows"
    return "linux"


def detect_chrome() -> Optional[Path]:
    """Best-effort Chrome / Chromium executable lookup per OS."""
    system = detect_system()
    candidates: list[Path] = []
    if system == "macos":
        candidates = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
            Path(
                "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary"
            ),
        ]
    elif system == "windows":
        program_files = [
            os.environ.get("PROGRAMFILES", r"C:\Program Files"),
            os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", ""),
        ]
        for pf in program_files:
            if pf:
                candidates.extend(
                    [
                        Path(pf) / r"Google\Chrome\Application\chrome.exe",
                        Path(pf) / r"Chromium\Application\chrome.exe",
                    ]
                )
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            found = shutil.which(name)
            if found:
                return Path(found)
        candidates = [
            Path("/usr/bin/google-chrome"),
            Path("/usr/bin/chromium"),
            Path("/snap/bin/chromium"),
        ]
    for c in candidates:
        if c.exists():
            return c
    return None


def claude_desktop_config_path() -> Path:
    """Resolve Claude Desktop's MCP config file path per OS.

    File may not exist yet — caller is responsible for handling that.
    """
    system = detect_system()
    if system == "macos":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "Claude"
            / "claude_desktop_config.json"
        )
    if system == "windows":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def cursor_config_path() -> Path:
    return Path.home() / ".cursor" / "mcp.json"


def default_profile_dir() -> Path:
    return Path.home() / DEFAULT_PROFILE_DIR_NAME


def livedocs_executable() -> Optional[Path]:
    """Locate the installed `livedocs-bridge` console script.

    We prefer the absolute path so generated MCP configs work even when the
    user's MCP client launches without their shell PATH (Claude Desktop on
    macOS, notably).
    """
    found = shutil.which("livedocs-bridge")
    if found:
        return Path(found)
    # Fall back to scripts dir of the running python (e.g. uvx-managed).
    scripts_dir = Path(sys.executable).parent
    candidate = scripts_dir / (
        "livedocs-bridge.exe" if detect_system() == "windows" else "livedocs-bridge"
    )
    if candidate.exists():
        return candidate
    return None


def collect_platform_info() -> PlatformInfo:
    return PlatformInfo(
        system=detect_system(),
        chrome_executable=detect_chrome(),
        claude_desktop_config=claude_desktop_config_path(),
        cursor_config=cursor_config_path(),
        default_profile_dir=default_profile_dir(),
        livedocs_executable=livedocs_executable(),
    )


def port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def find_free_port(preferred: int = DEFAULT_CDP_PORT, max_tries: int = 20) -> int:
    """Return `preferred` if free, else the next free port up to +max_tries."""
    for offset in range(max_tries):
        candidate = preferred + offset
        if port_is_free(candidate):
            return candidate
    # Last resort: let OS pick.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def cdp_endpoint_alive(url: str, timeout: float = 1.0) -> bool:
    """Probe `<url>/json/version` to see if CDP is actually accepting connections."""
    try:
        probe = url.rstrip("/") + "/json/version"
        with urlopen(probe, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except (URLError, TimeoutError, OSError):
        return False
