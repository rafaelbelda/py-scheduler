"""
task_parser.py — Converts raw config dicts into validated TaskDefinition objects.

Raises ValueError with a clear message for any invalid task config so
misconfiguration is caught at startup, not silently at runtime.
"""

from __future__ import annotations

import re
from typing import Any

from .models import Frequency, IntervalUnit, TaskDefinition

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TIME_RE = re.compile(r"^\d{2}:\d{2}$")
_DATE_YYYYMMDD_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATE_MMDD_RE = re.compile(r"^\d{2}-\d{2}$")

_DAY_NAME_MAP: dict[str, int] = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4,  "saturday": 5, "sunday": 6,
}


def _require(raw: dict, key: str, task_key: str) -> Any:
    if key not in raw or raw[key] is None:
        raise ValueError(f"task '{task_key}': required field '{key}' is missing")
    return raw[key]


def _validate_time(t: str, task_key: str) -> str:
    if not _TIME_RE.match(t):
        raise ValueError(
            f"task '{task_key}': invalid time '{t}' — expected HH:MM (24-hour)"
        )
    h, m = int(t[:2]), int(t[3:])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"task '{task_key}': time '{t}' is out of range")
    return t


def _parse_times(raw: dict, task_key: str) -> list[str]:
    raw_times = raw.get("times", [])
    if isinstance(raw_times, str):
        raw_times = [raw_times]
    if not raw_times:
        raise ValueError(f"task '{task_key}': 'times' must be a non-empty list")
    return [_validate_time(t, task_key) for t in raw_times]


def _parse_days_of_week(raw: dict, task_key: str) -> list[int]:
    raw_days = raw.get("days_of_week", [])
    if isinstance(raw_days, str):
        raw_days = [raw_days]
    if not raw_days:
        raise ValueError(f"task '{task_key}': 'days_of_week' must be a non-empty list")

    result: list[int] = []
    for d in raw_days:
        if isinstance(d, int) and 0 <= d <= 6:
            result.append(d)
        elif isinstance(d, str) and d.lower() in _DAY_NAME_MAP:
            result.append(_DAY_NAME_MAP[d.lower()])
        else:
            raise ValueError(
                f"task '{task_key}': invalid day_of_week '{d}' — "
                "use 0-6 or a name like 'monday'"
            )
    return result

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_task(task_key: str, raw: dict) -> TaskDefinition:
    """Parse and validate one raw task dict. Raises ValueError on bad input."""

    name    = str(raw.get("name", task_key))
    enabled = bool(raw.get("enabled", True))
    script  = str(_require(raw, "script", task_key))
    timeout = int(raw.get("timeout", 300))

    if timeout <= 0:
        raise ValueError(f"task '{task_key}': 'timeout' must be > 0")

    # --- frequency ---
    raw_freq = str(_require(raw, "frequency", task_key)).lower()
    try:
        frequency = Frequency(raw_freq)
    except ValueError:
        valid = [f.value for f in Frequency]
        raise ValueError(
            f"task '{task_key}': unknown frequency '{raw_freq}' — valid: {valid}"
        )

    notify_success = bool(raw.get("notify_on_success", False))
    notify_failure = bool(raw.get("notify_on_failure", True))

    # --- frequency-specific fields ---

    times:          list[str]       = []
    days_of_week:   list[int]       = []
    day_of_month:   int | None      = None
    month_day:      str | None      = None
    specific_date:  str | None      = None
    interval_unit:  IntervalUnit | None = None
    interval_value: int | None      = None

    if frequency == Frequency.DAILY:
        times = _parse_times(raw, task_key)

    elif frequency == Frequency.WEEKLY:
        days_of_week = _parse_days_of_week(raw, task_key)
        times        = _parse_times(raw, task_key)

    elif frequency == Frequency.MONTHLY:
        dom = raw.get("day_of_month")
        if dom is None:
            raise ValueError(f"task '{task_key}': 'day_of_month' required for monthly frequency")
        day_of_month = int(dom)
        if not (1 <= day_of_month <= 31):
            raise ValueError(f"task '{task_key}': 'day_of_month' must be 1–31")
        times = _parse_times(raw, task_key)

    elif frequency == Frequency.YEARLY:
        md = raw.get("month_day")
        if not md or not _DATE_MMDD_RE.match(str(md)):
            raise ValueError(
                f"task '{task_key}': 'month_day' required for yearly frequency "
                "(format: MM-DD, e.g. '01-15')"
            )
        month_day = str(md)
        times = _parse_times(raw, task_key)

    elif frequency == Frequency.INTERVAL:
        raw_unit = raw.get("interval_unit", "")
        try:
            interval_unit = IntervalUnit(str(raw_unit).lower())
        except ValueError:
            valid_units = [u.value for u in IntervalUnit]
            raise ValueError(
                f"task '{task_key}': invalid 'interval_unit' '{raw_unit}' — valid: {valid_units}"
            )

        iv = raw.get("interval_value")
        if iv is None:
            raise ValueError(f"task '{task_key}': 'interval_value' required for interval frequency")
        interval_value = int(iv)
        if interval_value <= 0:
            raise ValueError(f"task '{task_key}': 'interval_value' must be > 0")

    elif frequency in (Frequency.SPECIFIC, Frequency.ONCE):
        sd = raw.get("specific_date")
        if not sd or not _DATE_YYYYMMDD_RE.match(str(sd)):
            raise ValueError(
                f"task '{task_key}': 'specific_date' required for '{frequency.value}' frequency "
                "(format: YYYY-MM-DD)"
            )
        specific_date = str(sd)
        times = _parse_times(raw, task_key)

    return TaskDefinition(
        key            = task_key,
        name           = name,
        enabled        = enabled,
        script         = script,
        frequency      = frequency,
        timeout        = timeout,
        times          = times,
        days_of_week   = days_of_week,
        day_of_month   = day_of_month,
        month_day      = month_day,
        specific_date  = specific_date,
        interval_unit  = interval_unit,
        interval_value = interval_value,
        notify_on_success = notify_success,
        notify_on_failure = notify_failure,
    )


def parse_all_tasks(tasks_raw: dict[str, dict]) -> tuple[dict[str, TaskDefinition], list[str]]:
    """Parse all task entries. Returns (valid_tasks, error_messages).

    Does not raise — caller decides whether to abort or skip bad tasks.
    """
    tasks:  dict[str, TaskDefinition] = {}
    errors: list[str]                 = []

    for key, raw in tasks_raw.items():
        try:
            tasks[key] = parse_task(key, raw)
        except (ValueError, TypeError) as exc:
            errors.append(str(exc))

    return tasks, errors
