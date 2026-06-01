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
import hashlib
import itertools
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Optional

DEFAULT_KEEP_DAYS = 30
LAST_PUSH_PREFIX = "_last_pushed_"
BACKUP_PREFIX = "doc_backup_"

# v0.3.5: cap per-input size before feeding `difflib.unified_diff`. A
# pathological Doc body (multi-MB) would force `list(unified_diff(...))` to
# materialize hundreds of MB of diff lines before our line-cap truncated for
# display. Override via `LIVEDOCS_DRIFT_MAX_INPUT_BYTES`. Inputs larger than
# this trigger drift=True with a "too large to diff" summary — fail safe.
DEFAULT_DRIFT_MAX_INPUT_BYTES = 2 * 1024 * 1024  # 2 MB per side
# Also stream-cap the diff output itself so we never materialize more than
# this many lines even if the user raises `diff_max_lines` past it.
DRIFT_HARD_LINE_CAP = 5000

# Google Doc IDs are URL-safe base64 (`A-Za-z0-9_-`) and ~44 chars in practice.
# Anything outside this charset is either a corrupted URL or hostile input;
# we hash it down to a fixed-length safe key instead of writing it to disk.
_DOC_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_HASH_FALLBACK_LEN = 32  # SHA-256 hex truncated for filename brevity


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


def safe_doc_key(url_or_id: Optional[str]) -> str:
    """Return a filesystem-safe key for `url_or_id`.

    - Extracts the Doc id from a full URL if possible.
    - Returns the id verbatim when it matches `[A-Za-z0-9_-]{1,128}` (the only
      shape Google actually issues).
    - Otherwise returns `h_<sha256_hex[:32]>` so crafted inputs (with `/`, `..`,
      or other path separators) cannot escape the backup directory.

    The same key is used for both baselines and timestamped backups; the prior
    16-char truncation has been removed because two distinct Docs can share a
    16-char prefix, which led to cross-doc backup collisions (codex audit
    finding CRITICAL #1).
    """
    if not url_or_id:
        return "unknown"
    extracted = extract_doc_id(url_or_id) or url_or_id
    if _DOC_ID_RE.fullmatch(extracted):
        return extracted
    digest = hashlib.sha256(extracted.encode("utf-8")).hexdigest()
    return f"h_{digest[:_HASH_FALLBACK_LEN]}"


# Back-compat shims for any external callers still using the v0.3.0 names.
def doc_id_for_baseline(url_or_id: str) -> str:
    return safe_doc_key(url_or_id)


def doc_id_for_backup(url_or_id: str) -> str:
    return safe_doc_key(url_or_id)


def backup_base_path(
    backup_dir: Path,
    doc_url_or_id: str,
    timestamp: Optional[str] = None,
) -> Path:
    """Compute the `doc_backup_<ts>_<safe_id>` path (no suffix)."""
    ts = timestamp or time.strftime("%Y%m%d_%H%M%S")
    key = safe_doc_key(doc_url_or_id)
    backup_dir.mkdir(parents=True, exist_ok=True)
    return backup_dir / f"{BACKUP_PREFIX}{ts}_{key}"


def last_push_path(backup_dir: Path, doc_url_or_id: str) -> Path:
    """Path to `_last_pushed_<safe_doc_id>.txt`."""
    key = safe_doc_key(doc_url_or_id)
    return backup_dir / f"{LAST_PUSH_PREFIX}{key}.txt"


def atomic_write_text(path: Path, data: str) -> None:
    """Write `data` to `path` atomically via temp + rename.

    Prevents truncated baseline / backup files when the process dies mid-write
    (codex audit finding HIGH #3).

    v0.3.2: the temp file name is randomized via `tempfile.mkstemp` so two
    concurrent writers to the same target can't clobber each other's temp
    file mid-flight (verification audit partial → fully closed).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def save_last_push(
    text: str,
    doc_url_or_id: str,
    backup_dir: Optional[Path | str] = None,
) -> Path:
    """Persist `text` as the next drift baseline for this Doc (atomic)."""
    root = resolve_backup_dir(backup_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = last_push_path(root, doc_url_or_id)
    atomic_write_text(path, text)
    return path


def check_drift(
    current_plain: str,
    doc_url_or_id: str,
    backup_dir: Optional[Path | str] = None,
    diff_max_lines: int = 200,
) -> tuple[bool, str, dict]:
    """Compare `current_plain` against the saved baseline for this Doc.

    Returns:
        `(drifted, diff_summary, meta)` where `meta` has the shape
        `{hunks_total, hunks_shown, lines_total, lines_shown, truncated}`.

    Important: when no baseline exists yet (first inject for this Doc), we
    return `(False, "[no baseline ...]", {})` rather than `True`. Treating
    first-inject as drift would force every fresh Doc into `--force` mode.

    v0.3.4: returns a 3-tuple now (was 2-tuple in v0.3.0-v0.3.3). The third
    element is a hunk/line accounting dict so callers can surface "agent only
    saw N of M hunks" to a UI before the user approves `force=True`. The
    old 2-tuple silently truncated drift to the first 80 diff lines; a real
    production incident lost user edits because §4 hunks were past the cap
    and the agent assumed §6 was the only drifted section. Default cap also
    raised 80 → 200.
    """
    root = resolve_backup_dir(backup_dir)
    path = last_push_path(root, doc_url_or_id)
    if not path.exists():
        return False, "[no baseline — first inject for this doc]", {}
    last = path.read_text(encoding="utf-8")
    current = current_plain or ""
    if last.strip() == current.strip():
        return False, "", {}

    # v0.3.5 HIGH #1: hard-cap inputs BEFORE the diff is materialized. A
    # multi-MB Doc would otherwise OOM the agent. We still report drift, but
    # the summary just says "too large to diff" with the input sizes — caller
    # must decide via screenshot / manual inspection.
    max_bytes = _max_diff_input_bytes()
    if (
        len(last.encode("utf-8")) > max_bytes
        or len(current.encode("utf-8")) > max_bytes
    ):
        summary = (
            f"⚠ DRIFT DETECTED BUT INPUT TOO LARGE TO DIFF: baseline="
            f"{len(last)} chars, current={len(current)} chars (cap "
            f"{max_bytes // 1024} KiB per side). `force=True` overwrites "
            f"ALL drift including content this summary doesn't show. "
            f"Inspect the Doc visually before deciding."
        )
        return True, summary, {
            "hunks_total": -1,
            "hunks_shown": 0,
            "lines_total": -1,
            "lines_shown": 0,
            "truncated": True,
            "max_input_exceeded": True,
            "baseline_chars": len(last),
            "current_chars": len(current),
        }

    # Stream-cap the diff iterator so we never materialize more than
    # DRIFT_HARD_LINE_CAP lines, even if the diff itself would be enormous.
    # We still need to count hunks_total beyond the displayed slice, so we
    # iterate twice: once up to diff_max_lines for the display copy, then
    # continue the same iterator counting `@@` lines until the hard cap.
    diff_iter = difflib.unified_diff(
        last.splitlines(),
        current.splitlines(),
        fromfile="last_pushed",
        tofile="current_doc",
        lineterm="",
        n=2,
    )
    capped_iter = itertools.islice(diff_iter, DRIFT_HARD_LINE_CAP + 1)
    shown_lines: list[str] = []
    hunks_shown = 0
    hunks_total = 0
    lines_total = 0
    for ln in capped_iter:
        lines_total += 1
        is_hunk = ln.startswith("@@")
        if is_hunk:
            hunks_total += 1
        if len(shown_lines) < diff_max_lines:
            shown_lines.append(ln)
            if is_hunk:
                hunks_shown += 1
    hard_capped = lines_total > DRIFT_HARD_LINE_CAP
    if hard_capped:
        # Subtract the sentinel line we read just to detect the overflow.
        lines_total = DRIFT_HARD_LINE_CAP
    truncated = lines_total > len(shown_lines) or hard_capped

    summary_parts: list[str] = []
    if truncated:
        if hard_capped:
            count_phrase = (
                f"at least {hunks_total} hunks "
                f"(stream-capped at {DRIFT_HARD_LINE_CAP} diff lines; "
                f"true hunk count is higher)"
            )
        else:
            count_phrase = (
                f"{hunks_shown} of {hunks_total} hunks "
                f"({len(shown_lines)} of {lines_total} diff lines)"
            )
        summary_parts.append(
            f"⚠ DRIFT TRUNCATED: showing {count_phrase}. "
            f"`force=True` overwrites ALL drift including the hidden hunks."
        )
        summary_parts.append("")
    summary_parts.append("\n".join(shown_lines))
    if truncated:
        if hard_capped:
            summary_parts.append(
                f"... (stream-capped at {DRIFT_HARD_LINE_CAP} diff lines; "
                "remaining hunks not counted)"
            )
        else:
            summary_parts.append(
                f"... ({lines_total - len(shown_lines)} more diff lines, "
                f"{hunks_total - hunks_shown} more hunks hidden)"
            )
    meta = {
        "hunks_total": hunks_total,
        "hunks_shown": hunks_shown,
        "lines_total": lines_total,
        "lines_shown": len(shown_lines),
        "truncated": truncated,
        "hard_capped": hard_capped,
    }
    return True, "\n".join(summary_parts), meta


def _max_diff_input_bytes() -> int:
    raw = os.environ.get("LIVEDOCS_DRIFT_MAX_INPUT_BYTES")
    if not raw:
        return DEFAULT_DRIFT_MAX_INPUT_BYTES
    try:
        n = int(raw)
        return n if n > 0 else DEFAULT_DRIFT_MAX_INPUT_BYTES
    except (TypeError, ValueError):
        return DEFAULT_DRIFT_MAX_INPUT_BYTES


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

    Filters by the full safe doc key (post v0.3.1). The pre-v0.3.1 16-char
    truncation has been removed — two distinct Docs can no longer collide.
    """
    root = resolve_backup_dir(backup_dir)
    if not root.exists():
        return []
    key = safe_doc_key(doc_url_or_id)
    suffix_marker = f"_{key}"
    grouped: dict[str, dict] = {}
    for f in root.iterdir():
        if not f.is_file() or not f.name.startswith(BACKUP_PREFIX):
            continue
        if not f.stem.endswith(suffix_marker):
            continue
        # Strip prefix + trailing _<key>; what remains is the timestamp.
        stem = f.stem
        ts = stem[len(BACKUP_PREFIX) : -len(suffix_marker)]
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
