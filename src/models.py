"""
models.py — Dataclasses that flow through the entire scheduler.

Nothing here has side-effects; safe to import anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Frequency(str, Enum):
    DAILY    = "daily"
    WEEKLY   = "weekly"
    MONTHLY  = "monthly"
    YEARLY   = "yearly"
    INTERVAL = "interval"   # every N minutes / hours / days
    SPECIFIC = "specific"   # one specific date, repeats yearly=False
    ONCE     = "once"       # run once, then auto-disable


class IntervalUnit(str, Enum):
    MINUTES = "minutes"
    HOURS   = "hours"
    DAYS    = "days"


class RunStatus(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"


# ---------------------------------------------------------------------------
# Task definition (parsed from config.json)
# ---------------------------------------------------------------------------

@dataclass
class TaskDefinition:
    """Validated representation of one entry under config["tasks"]."""

    key: str                    # the dict key in config.json, e.g. "backup"
    name: str                   # human-readable label
    enabled: bool
    script: str                 # path relative to project root, e.g. "tasks/backup.py"
    frequency: Frequency
    timeout: int                # seconds; default 300

    # --- daily / weekly / monthly / yearly / specific / once ---
    times: list[str] = field(default_factory=list)   # ["07:00", "18:00"]
    days_of_week: list[int] = field(default_factory=list)  # 0=Mon … 6=Sun
    day_of_month: int | None = None                  # 1-31 for monthly
    month_day: str | None = None                     # "MM-DD" for yearly
    specific_date: str | None = None                 # "YYYY-MM-DD" for specific/once

    # --- interval ---
    interval_unit: IntervalUnit | None = None
    interval_value: int | None = None                # e.g. 30 (minutes)

    # --- notifications ---
    notify_on_success: bool = False
    notify_on_failure: bool = True

    # --- metadata (runtime, not from config) ---
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Run result (returned by runner, written to log)
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    """The outcome of a single task execution attempt."""

    task_key:   str
    task_name:  str
    status:     RunStatus
    exit_code:  int | None
    duration_s: float
    started_at: datetime
    stdout:     str | None  = None
    stderr:     str | None  = None
    detail:     str | None  = None   # human-readable summary line
    forced:     bool        = False  # True when triggered via API

    # Convenience ---------------------------------------------------------

    @property
    def ok(self) -> bool:
        return self.status == RunStatus.SUCCESS

    def to_log_dict(self) -> dict[str, Any]:
        """Serialisable dict written as one JSONL line."""
        return {
            "ts":         self.started_at.isoformat(),
            "task_key":   self.task_key,
            "task_name":  self.task_name,
            "status":     self.status.value,
            "exit_code":  self.exit_code,
            "duration_s": round(self.duration_s, 3),
            "forced":     self.forced,
            "stdout":     self.stdout,
            "stderr":     self.stderr,
            "detail":     self.detail,
        }
