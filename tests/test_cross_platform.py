"""v0.3.3 — Windows / Linux keyboard regression coverage.

The original bug (silent paste no-op on Windows) was caused by hardcoded
`Meta+` keystrokes. Playwright maps `Meta` to Win on Windows, so `Meta+V`
opened the clipboard history overlay instead of pasting. These tests fail
loudly if anyone reintroduces `Meta+` (without `OrMeta`) into the codebase.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest

from livedocs_bridge import tools

PKG_DIR = Path(__file__).resolve().parent.parent / "livedocs_bridge"
SOURCES = sorted(PKG_DIR.glob("*.py"))

# Match `keyboard.press("Meta+...")` — but NOT `ControlOrMeta+...`.
_BAD_META = re.compile(r'keyboard\.press\("(?!ControlOrMeta)Meta\+')


@pytest.mark.parametrize("source", SOURCES, ids=lambda p: p.name)
def test_no_naked_meta_keypress(source):
    """Reject any new `keyboard.press("Meta+...")` site.

    `Meta` alone resolves to Win on Windows. Use `ControlOrMeta` (cross-platform
    alias) or the explicit `_find_replace_shortcut()` helper when the Shift
    component also differs by OS.
    """
    text = source.read_text(encoding="utf-8")
    # tools.py is allowed exactly one literal `Meta+Shift+H` — it's inside the
    # `_find_replace_shortcut()` helper that branches by `platform.system()`.
    if source.name == "tools.py":
        allowed = text.count('"Meta+Shift+H"')
        assert allowed <= 1, (
            f"{source.name} has more than one Meta+Shift+H literal; "
            "verify they're all inside platform-branching code."
        )
    else:
        # Other modules must not use bare Meta+ keypresses at all.
        matches = _BAD_META.findall(text)
        assert matches == [], (
            f"{source.name} contains bare Meta+ keypresses: {matches}. "
            "Use ControlOrMeta+<key> for cross-platform keyboard ops."
        )


def test_find_replace_shortcut_mac(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    assert tools._find_replace_shortcut() == "Meta+Shift+H"


def test_find_replace_shortcut_windows(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Windows")
    assert tools._find_replace_shortcut() == "Control+H"


def test_find_replace_shortcut_linux(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Linux")
    assert tools._find_replace_shortcut() == "Control+H"


def test_playwright_core_uses_control_or_meta_everywhere():
    """Sanity check: every cross-platform keypress in playwright_core.py uses
    the `ControlOrMeta` prefix. The grep above caught the negative case; this
    one counts the positive case so the keypress sites can't silently vanish."""
    text = (PKG_DIR / "playwright_core.py").read_text(encoding="utf-8")
    cross_platform_presses = re.findall(r'keyboard\.press\("ControlOrMeta\+', text)
    # 7 sites: clear_doc (1) + capture_doc_plain (2) + backup_doc (2)
    # + move_caret_to_end (1) + paste_html (1).
    assert len(cross_platform_presses) == 7, (
        f"Expected 7 ControlOrMeta+ keypresses in playwright_core.py, "
        f"found {len(cross_platform_presses)}. If a helper was deleted, "
        "update this count; if one was added, prefer ControlOrMeta+."
    )
