"""Drift detection + persistent backups for destructive Doc operations.

Why this module exists (v0.3.0, source: HertzFlow × WLFI memo session 2026-05-30/31):

Wholesale-replace is destructive by default. If the user (or a teammate) edits
the Doc in their browser between two agent injects, the second inject silently
overwrites those edits. Production users hit this hard during real workflows.

The fix is mandatory, not optional:
1. Before any destructive op, write the current Doc text + HTML to a persistent
   backup directory.
2. Compare the current Doc text to the snapshot we saved the last time we
   pushed. If they don't match, the Doc has drifted under us — abort unless
   the caller explicitly passes force=True.
3. After a successful push, save the new content as the next baseline.

Backup files live under a platform-appropriate persistent path (NOT /tmp,
which gets purged on macOS reboot). 30-day rotation is built in; the
`_last_pushed_*` baselines are never pruned.
"""

from __future__ import annotations

import difflib
import os
import time
from pathlib import Path
from typing import Optional

DOC_ID_TRUNC = 16
DEFAULT_KEEP_DAYS = 30
LAST_PUSH_PREFIX = "_last_pushed_"
BACKUP_PREFIX = "doc_backup_"


def default_backup_dir() -> Path:
    """Return the persistent backup root for this platform.

    Override at runtime with `LIVEDOCS_BACKUP_DIR=/abs/path`.
    """
    env = os.environ.get("LIVEDOCS_BACKUP_DIR")
    if env:
        return Path(env).expanduser()
    # Single cross-platform default. Keeps the install footprint predictable
    # and survives macOS reboots (unlike /tmp).
    return Path.home() / ".livedocs-bridge" / "backups"


def resolve_backup_dir(backup_dir: Optional[Path | str] = None) -> Path:
    if backup_dir is None:
        return default_backup_dir()
    return Path(backup_dir).expanduser()


def extract_doc_id(url: str) -> Optional[str]:
    """Pull the Google Doc id out of a `/document/d/<id>/...` URL."""
    try:
        return url.split("/document/d/", 1)[1].split("/", 1)[0].split("?", 1)[0]
    except (IndexError, AttributeError):
        return None


def doc_id_for_baseline(url_or_id: str) -> str:
    """Return the canonical identifier used in `_last_pushed_<id>.txt`.

    Accepts either a full Doc URL or a bare id. Full ids are kept verbatim
    so baselines round-trip across sessions; the truncation only applies to
    timestamped backups (to keep filenames short on Windows).
    """
    extracted = extract_doc_id(url_or_id) or url_or_id
    return extracted


def doc_id_for_backup(url_or_id: str) -> str:
    return doc_id_for_baseline(url_or_id)[:DOC_ID_TRUNC] or "unknown"


def backup_base_path(
    backup_dir: Path,
    doc_url_or_id: str,
    timestamp: Optional[str] = None,
) -> Path:
    """Compute the `doc_backup_<ts>_<short_id>` path (no suffix)."""
    ts = timestamp or time.strftime("%Y%m%d_%H%M%S")
    short = doc_id_for_backup(doc_url_or_id)
    backup_dir.mkdir(parents=True, exist_ok=True)
    return backup_dir / f"{BACKUP_PREFIX}{ts}_{short}"


def last_push_path(backup_dir: Path, doc_url_or_id: str) -> Path:
    """Path to `_last_pushed_<full_doc_id>.txt`."""
    full_id = doc_id_for_baseline(doc_url_or_id) or "unknown"
    return backup_dir / f"{LAST_PUSH_PREFIX}{full_id}.txt"


def save_last_push(
    text: str,
    doc_url_or_id: str,
    backup_dir: Optional[Path | str] = None,
) -> Path:
    """Persist `text` as the next drift baseline for this Doc."""
    root = resolve_backup_dir(backup_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = last_push_path(root, doc_url_or_id)
    path.write_text(text, encoding="utf-8")
    return path


def check_drift(
    current_plain: str,
    doc_url_or_id: str,
    backup_dir: Optional[Path | str] = None,
    diff_max_lines: int = 80,
) -> tuple[bool, str]:
    """Compare `current_plain` against the saved baseline for this Doc.

    Returns:
        (drifted, diff_summary).

    Important: when no baseline exists yet (first inject for this Doc), we
    return `(False, "[no baseline ...]")` rather than `True`. Treating
    first-inject as drift would force every fresh Doc into `--force` mode.
    """
    root = resolve_backup_dir(backup_dir)
    path = last_push_path(root, doc_url_or_id)
    if not path.exists():
        return False, "[no baseline — first inject for this doc]"
    last = path.read_text(encoding="utf-8")
    if last.strip() == (current_plain or "").strip():
        return False, ""
    diff_lines = list(
        difflib.unified_diff(
            last.splitlines(),
            (current_plain or "").splitlines(),
            fromfile="last_pushed",
            tofile="current_doc",
            lineterm="",
            n=2,
        )
    )
    summary = "\n".join(diff_lines[:diff_max_lines])
    if len(diff_lines) > diff_max_lines:
        summary += f"\n... ({len(diff_lines) - diff_max_lines} more diff lines truncated)"
    return True, summary


def prune_old_backups(
    backup_dir: Optional[Path | str] = None,
    keep_days: int = DEFAULT_KEEP_DAYS,
) -> int:
    """Delete `doc_backup_*` files older than `keep_days`. Never touches `_last_pushed_*`.

    Returns the count of files removed.
    """
    root = resolve_backup_dir(backup_dir)
    if not root.exists():
        return 0
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for f in root.iterdir():
        if not f.is_file():
            continue
        if f.name.startswith("_"):  # protect _last_pushed_*, _state, etc.
            continue
        if not f.name.startswith(BACKUP_PREFIX):
            continue
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def list_backups(
    doc_url_or_id: str,
    backup_dir: Optional[Path | str] = None,
) -> list[dict]:
    """Return all backups for a Doc, newest first.

    Each entry: {"timestamp": str, "txt": Path|None, "html": Path|None, "mtime": float}.
    """
    root = resolve_backup_dir(backup_dir)
    if not root.exists():
        return []
    short = doc_id_for_backup(doc_url_or_id)
    grouped: dict[str, dict] = {}
    for f in root.iterdir():
        if not f.is_file() or not f.name.startswith(BACKUP_PREFIX):
            continue
        if not f.stem.endswith(f"_{short}"):
            continue
        # Strip prefix + trailing _<short>; what remains is the timestamp.
        stem = f.stem
        ts = stem[len(BACKUP_PREFIX) : -(len(short) + 1)]
        entry = grouped.setdefault(
            ts, {"timestamp": ts, "txt": None, "html": None, "mtime": 0.0}
        )
        if f.suffix == ".txt":
            entry["txt"] = f
        elif f.suffix == ".html":
            entry["html"] = f
        try:
            entry["mtime"] = max(entry["mtime"], f.stat().st_mtime)
        except OSError:
            pass
    return sorted(grouped.values(), key=lambda e: e["mtime"], reverse=True)


def find_backup(
    doc_url_or_id: str,
    timestamp: Optional[str] = None,
    backup_dir: Optional[Path | str] = None,
) -> Optional[dict]:
    """Return the requested backup entry, or the latest if `timestamp` is None."""
    backups = list_backups(doc_url_or_id, backup_dir)
    if not backups:
        return None
    if timestamp is None:
        return backups[0]
    for entry in backups:
        if entry["timestamp"] == timestamp:
            return entry
    return None
