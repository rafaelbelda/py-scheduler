"""
logger.py — Writes task run results to daily JSONL log files.

File layout
-----------
    logs/
        2025-01-15.jsonl
        2025-01-16.jsonl
        ...

Each line is one JSON object produced by RunResult.to_log_dict().

Public API
----------
    task_logger.write(result)                  — append one RunResult
    task_logger.read(date_str)                 — all entries for YYYY-MM-DD
    task_logger.read_range(start, end)         — entries across a date range
    task_logger.prune(retention_days)          — delete files older than N days
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

from .models import RunResult

logger = logging.getLogger(__name__)

# One lock per log file (keyed by YYYY-MM-DD string) prevents interleaved
# writes when multiple tasks complete simultaneously.
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _get_lock(date_str: str) -> threading.Lock:
    with _locks_guard:
        if date_str not in _locks:
            _locks[date_str] = threading.Lock()
        return _locks[date_str]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _log_dir(project_root: Path) -> Path:
    """Resolve log directory from config, falling back to <root>/logs."""
    try:
        import src.config as config  # noqa: PLC0415
        rel = config.get("scheduler.log_dir") or "logs"
    except Exception:
        rel = "logs"
    p = Path(rel)
    return p if p.is_absolute() else project_root / p


def _log_path(project_root: Path, date_str: str) -> Path:
    return _log_dir(project_root) / f"{date_str}.jsonl"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def write(result: RunResult, project_root: Path) -> None:
    """Append *result* as a single JSON line to today's log file.

    Thread-safe: multiple tasks can call this concurrently.
    """
    date_str = result.started_at.strftime("%Y-%m-%d")
    path     = _log_path(project_root, date_str)
    lock     = _get_lock(date_str)

    path.parent.mkdir(parents=True, exist_ok=True)

    line = json.dumps(result.to_log_dict(), ensure_ascii=False)

    with lock:
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError as exc:
            logger.error("failed to write task log for '%s': %s", result.task_key, exc)


def read(date_str: str, project_root: Path) -> list[dict]:
    """Return all log entries for a given *date_str* (``YYYY-MM-DD``).

    Returns an empty list if the file does not exist or cannot be parsed.
    """
    path = _log_path(project_root, date_str)

    if not path.exists():
        return []

    entries: list[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                entries.append(json.loads(raw))
            except json.JSONDecodeError as exc:
                logger.warning(
                    "skipping malformed log line %d in %s: %s", lineno, path.name, exc
                )
    return entries


def read_range(
    start: date | str,
    end: date | str,
    project_root: Path,
    *,
    task_key: str | None = None,
) -> list[dict]:
    """Return entries across an inclusive date range.

    Args:
        start, end:  ``date`` objects or ``"YYYY-MM-DD"`` strings.
        task_key:    Optional filter — only return entries for this task.
    """
    def _to_date(val: date | str) -> date:
        if isinstance(val, date):
            return val
        return datetime.strptime(val, "%Y-%m-%d").date()

    d      = _to_date(start)
    end_d  = _to_date(end)
    result : list[dict] = []

    while d <= end_d:
        for entry in read(d.strftime("%Y-%m-%d"), project_root):
            if task_key is None or entry.get("task_key") == task_key:
                result.append(entry)
        d += timedelta(days=1)

    return result


def prune(retention_days: int, project_root: Path) -> int:
    """Delete log files older than *retention_days* days.

    Args:
        retention_days: 0 means keep forever (no-op).

    Returns:
        Number of files deleted.
    """
    if retention_days <= 0:
        return 0

    cutoff  = date.today() - timedelta(days=retention_days)
    log_dir = _log_dir(project_root)
    deleted = 0

    if not log_dir.exists():
        return 0

    for path in log_dir.glob("*.jsonl"):
        try:
            file_date = datetime.strptime(path.stem, "%Y-%m-%d").date()
        except ValueError:
            continue  # not a date-named file — ignore

        if file_date < cutoff:
            try:
                path.unlink()
                logger.info("pruned old log file: %s", path.name)
                deleted += 1
            except OSError as exc:
                logger.error("failed to prune %s: %s", path.name, exc)

    return deleted
