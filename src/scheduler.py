"""
scheduler.py — The core scheduling loop.

Design decisions
----------------
* Tick granularity is 1 minute. The loop sleeps to the *next* wall-clock
  minute boundary so ticks are aligned to HH:MM regardless of startup time
  or how long previous tasks took.

* Tasks run in a ThreadPoolExecutor with a configurable max_workers cap
  (config: scheduler.max_workers, default 4).  Each submitted task gets its
  own thread so slow tasks never block the tick loop.

* A threading.Event (_stop_event) allows clean shutdown from any thread.

* The _last_run dict tracks the last *scheduled* (non-forced) execution time
  per task key so the "already ran this minute" guard works correctly for
  tasks with multiple execute times.

* Config is re-read from the in-memory cache each tick, so tasks can be
  enabled/disabled via the API without a restart.  A full config.reload()
  is required to pick up changes written directly to config.json.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import src.config as config
import src.task_logger as task_logger
from .models import RunResult, RunStatus, TaskDefinition, Frequency, IntervalUnit
from .notifier import notify_result
from .runner import run_task
from .task_parser import parse_all_tasks

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Due-time logic (pure functions — easy to unit-test)
# ---------------------------------------------------------------------------

def _is_time_due(time_str: str, now: datetime) -> bool:
    """Return True if the HH:MM time_str matches the current wall-clock minute."""
    try:
        h, m = int(time_str[:2]), int(time_str[3:])
    except (ValueError, IndexError):
        return False
    return now.hour == h and now.minute == m


def _interval_due(task: TaskDefinition, now: datetime) -> bool:
    """
    Midnight-anchored interval check.

    Divides the day (minutes since midnight) by the interval period and
    checks whether *now* falls on a boundary.

    Examples (interval_value=6, interval_unit=hours):
      Fires at 00:00, 06:00, 12:00, 18:00.
    """
    unit  = task.interval_unit
    value = task.interval_value

    if unit == IntervalUnit.MINUTES:
        period_minutes = value
    elif unit == IntervalUnit.HOURS:
        period_minutes = value * 60
    elif unit == IntervalUnit.DAYS:
        period_minutes = value * 1440
    else:
        return False

    minutes_since_midnight = now.hour * 60 + now.minute
    return minutes_since_midnight % period_minutes == 0


def _is_due(task: TaskDefinition, now: datetime, last_run: dict[str, datetime]) -> str | None:
    """
    Return the *time_str* trigger that is due this minute, or None.

    For interval tasks the returned string is "interval" (used as the run_key
    component but carries no extra meaning).

    The caller must check _last_run to avoid double-firing within the same minute.
    """
    if not task.enabled:
        return None

    freq = task.frequency

    # ---- interval -------------------------------------------------------
    if freq == Frequency.INTERVAL:
        if not _interval_due(task, now):
            return None
        run_key = f"{task.key}:interval"
        last = last_run.get(run_key)
        if last and last.year == now.year and last.timetuple().tm_yday == now.timetuple().tm_yday \
                and last.hour == now.hour and last.minute == now.minute:
            return None
        return "interval"

    # ---- date / day-of-week guards --------------------------------------
    if freq == Frequency.WEEKLY:
        if now.weekday() not in task.days_of_week:
            return None

    elif freq == Frequency.MONTHLY:
        if now.day != task.day_of_month:
            return None

    elif freq == Frequency.YEARLY:
        expected = task.month_day  # "MM-DD"
        if now.strftime("%m-%d") != expected:
            return None

    elif freq in (Frequency.SPECIFIC, Frequency.ONCE):
        if now.strftime("%Y-%m-%d") != task.specific_date:
            return None

    # ---- time-of-day check (all non-interval frequencies) ---------------
    for time_str in task.times:
        if _is_time_due(time_str, now):
            run_key = f"{task.key}:{time_str}"
            last = last_run.get(run_key)
            if last and last.year == now.year \
                    and last.timetuple().tm_yday == now.timetuple().tm_yday \
                    and last.hour == now.hour and last.minute == now.minute:
                continue  # already fired this minute
            return time_str

    return None


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

class Scheduler:
    """
    Manages the tick loop and dispatches due tasks to a thread pool.

    Lifecycle
    ---------
        scheduler = Scheduler(project_root)
        scheduler.start()          # launches background thread
        ...
        scheduler.stop()           # signals stop; joins loop thread

    Manual execution (API)
    ----------------------
        future = scheduler.run_now(task_key)
        result = future.result()   # blocks caller until done
    """

    def __init__(self, project_root: Path) -> None:
        self._root        = project_root
        self._stop_event  = threading.Event()
        self._loop_thread : threading.Thread | None = None

        # last_run tracks per-run-key (task_key:time_str) datetime objects
        self._last_run: dict[str, datetime] = {}
        self._last_run_lock = threading.Lock()

        # populated on start / on reload
        self._tasks: dict[str, TaskDefinition] = {}
        self._tasks_lock = threading.Lock()

        # thread pool for task execution
        max_workers = int(config.get("scheduler.max_workers") or 4)
        self._executor = ThreadPoolExecutor(
            max_workers = max_workers,
            thread_name_prefix = "task-worker",
        )

        self._load_tasks(first_run=True)

    # ------------------------------------------------------------------
    # Task loading
    # ------------------------------------------------------------------

    def _load_tasks(self, first_run: bool = False) -> None:
        raw_tasks = config.get("tasks") or {}
        tasks, errors = parse_all_tasks(raw_tasks)

        for err in errors:
            logger.error("task config error: %s", err)

        with self._tasks_lock:
            self._tasks = tasks

        if first_run:
            logger.info(
                "loaded %d task(s)%s",
                len(tasks),
                f" ({len(errors)} error(s))" if errors else "",
            )
        else:
            logger.debug(
                "reloaded %d task(s)%s",
                len(tasks),
                f" ({len(errors)} error(s))" if errors else "",
            )

    def reload_tasks(self) -> None:
        """Re-read tasks from the in-memory config (call after config.reload())."""
        self._load_tasks()

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the scheduler loop in a daemon background thread."""
        if self._loop_thread and self._loop_thread.is_alive():
            logger.warning("scheduler already running")
            return

        self._stop_event.clear()
        self._loop_thread = threading.Thread(
            target     = self._loop,
            name       = "scheduler-loop",
            daemon     = True,
        )
        self._loop_thread.start()
        logger.info("scheduler started")

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the loop to stop and wait for it to exit."""
        self._stop_event.set()
        if self._loop_thread:
            self._loop_thread.join(timeout=timeout)
        logger.info("scheduler stopped")

    @property
    def is_running(self) -> bool:
        return bool(self._loop_thread and self._loop_thread.is_alive())

    # ------------------------------------------------------------------
    # Manual execution (API)
    # ------------------------------------------------------------------

    def run_now(self, task_key: str) -> Future[RunResult]:
        """Submit a task for immediate execution and return a Future.

        Raises KeyError if task_key is not found.
        The task is marked forced=True so notifications are suppressed.
        """
        with self._tasks_lock:
            task = self._tasks.get(task_key)

        if task is None:
            raise KeyError(f"unknown task key: '{task_key}'")

        return self._executor.submit(self._execute, task, forced=True)

    def get_tasks(self) -> dict[str, TaskDefinition]:
        with self._tasks_lock:
            return dict(self._tasks)

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        logger.debug("scheduler loop entered")

        # Prune old logs once at startup
        retention = int(config.get("scheduler.log_retention_days") or 0)
        if retention > 0:
            deleted = task_logger.prune(retention, self._root)
            if deleted:
                logger.info("pruned %d old log file(s)", deleted)

        while not self._stop_event.is_set():
            # Sleep to the start of the next wall-clock minute.
            now          = datetime.now()
            next_minute  = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
            sleep_secs   = (next_minute - now).total_seconds()

            # Use Event.wait so that stop() can interrupt the sleep immediately.
            if self._stop_event.wait(timeout=max(sleep_secs, 0.1)):
                break  # stop was requested

            tick_time = datetime.now()

            # Re-read tasks each tick so enable/disable changes take effect.
            # Parsing is cheap (dict comprehension); no disk I/O here.
            self._load_tasks()

            with self._tasks_lock:
                tasks_snapshot = dict(self._tasks)

            with self._last_run_lock:
                last_run_snapshot = dict(self._last_run)

            for task in tasks_snapshot.values():
                try:
                    trigger = _is_due(task, tick_time, last_run_snapshot)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("error checking due status for '%s': %s", task.key, exc)
                    continue

                if trigger is not None:
                    run_key = f"{task.key}:{trigger}"
                    with self._last_run_lock:
                        self._last_run[run_key] = tick_time

                    self._executor.submit(self._execute, task, forced=False)
                    logger.debug("submitted task '%s' (trigger: %s)", task.key, trigger)

        logger.debug("scheduler loop exited")

    # ------------------------------------------------------------------
    # Task execution (runs inside worker thread)
    # ------------------------------------------------------------------

    def _execute(self, task: TaskDefinition, *, forced: bool) -> RunResult:
        result = run_task(task, self._root, forced=forced)

        # Write to JSONL log
        try:
            task_logger.write(result, self._root)
        except Exception as exc:  # noqa: BLE001
            logger.error("failed to write log for task '%s': %s", task.key, exc)

        # Handle ONCE — auto-disable after successful run
        if task.frequency == Frequency.ONCE and result.ok and not forced:
            try:
                config.set(f"tasks.{task.key}.enabled", False)
                logger.info("task '%s' (once) completed — auto-disabled", task.key)
            except Exception as exc:  # noqa: BLE001
                logger.error("failed to auto-disable once-task '%s': %s", task.key, exc)

        # Notify (ntfy)
        try:
            notify_result(result)
        except Exception as exc:  # noqa: BLE001
            logger.error("notification error for task '%s': %s", task.key, exc)

        return result