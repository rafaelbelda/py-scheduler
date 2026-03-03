"""
runner.py — Executes a task script in a subprocess and returns a RunResult.

Completely decoupled from scheduling logic. Can be called from:
  - The scheduler loop (normal execution)
  - The API (forced/manual execution)
  - Tests
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from .models import RunResult, RunStatus, TaskDefinition

logger = logging.getLogger(__name__)


def run_task(task: TaskDefinition, project_root: Path, forced: bool = False) -> RunResult:
    """Execute *task* as a subprocess and return a RunResult.

    The script is run as a module (``python -m <module>``) so that relative
    imports within the tasks package work correctly.

    Args:
        task:         The validated task definition.
        project_root: Absolute path to the project root (parent of ``src/``).
        forced:       True when the run was triggered manually (API call).

    Returns:
        A RunResult with status, timing, stdout/stderr, etc.
    """

    script_path = project_root / task.script

    if not script_path.exists():
        logger.error("task '%s': script not found at %s", task.key, script_path)
        return RunResult(
            task_key   = task.key,
            task_name  = task.name,
            status     = RunStatus.FAILURE,
            exit_code  = None,
            duration_s = 0.0,
            started_at = datetime.now(),
            detail     = f"script not found: {task.script}",
            forced     = forced,
        )

    # Build the dotted module name from the script path relative to project root.
    # e.g. tasks/backup.py  →  tasks.backup
    try:
        module_name = (
            script_path
            .relative_to(project_root)
            .with_suffix("")
            .as_posix()
            .replace("/", ".")
        )
    except ValueError:
        logger.error(
            "task '%s': script '%s' is not inside project root '%s'",
            task.key, script_path, project_root,
        )
        return RunResult(
            task_key   = task.key,
            task_name  = task.name,
            status     = RunStatus.FAILURE,
            exit_code  = None,
            duration_s = 0.0,
            started_at = datetime.now(),
            detail     = f"script '{task.script}' must be inside the project root",
            forced     = forced,
        )

    logger.info("starting task '%s' (module: %s, forced=%s)", task.key, module_name, forced)

    started_at  = datetime.now()
    mono_start  = time.monotonic()

    try:
        result = subprocess.run(
            [sys.executable, "-m", module_name],
            cwd           = project_root,
            text          = True,
            capture_output = True,
            timeout       = task.timeout,
            env           = {**os.environ},   # inherit env, explicit copy
        )

        duration_s = time.monotonic() - mono_start
        stdout     = result.stdout.strip() or None
        stderr     = result.stderr.strip() or None

        if result.returncode == 0:
            detail = f"exited 0 in {duration_s:.2f}s"
            if stdout:
                detail += f" | stdout: {stdout[:200]}"  # keep detail line readable
            logger.info("task '%s' succeeded in %.2fs", task.key, duration_s)
            return RunResult(
                task_key   = task.key,
                task_name  = task.name,
                status     = RunStatus.SUCCESS,
                exit_code  = 0,
                duration_s = duration_s,
                started_at = started_at,
                stdout     = stdout,
                stderr     = stderr,
                detail     = detail,
                forced     = forced,
            )
        else:
            detail = f"exited {result.returncode} in {duration_s:.2f}s"
            logger.error(
                "task '%s' failed (rc=%d) in %.2fs — stderr: %s",
                task.key, result.returncode, duration_s, (stderr or "")[:400],
            )
            return RunResult(
                task_key   = task.key,
                task_name  = task.name,
                status     = RunStatus.FAILURE,
                exit_code  = result.returncode,
                duration_s = duration_s,
                started_at = started_at,
                stdout     = stdout,
                stderr     = stderr,
                detail     = detail,
                forced     = forced,
            )

    except subprocess.TimeoutExpired:
        # subprocess.run kills the child process before raising TimeoutExpired
        # (it calls proc.kill() internally), so no manual cleanup needed.
        duration_s = time.monotonic() - mono_start
        detail     = f"timed out after {duration_s:.1f}s (limit: {task.timeout}s)"
        logger.error("task '%s' timed out after %.1fs", task.key, duration_s)
        return RunResult(
            task_key   = task.key,
            task_name  = task.name,
            status     = RunStatus.TIMEOUT,
            exit_code  = 124,   # standard timeout exit code (same as GNU timeout)
            duration_s = duration_s,
            started_at = started_at,
            detail     = detail,
            forced     = forced,
        )

    except Exception as exc:  # noqa: BLE001
        duration_s = time.monotonic() - mono_start
        detail     = f"{type(exc).__name__}: {exc}"
        logger.exception("unexpected error running task '%s': %s", task.key, exc)
        return RunResult(
            task_key   = task.key,
            task_name  = task.name,
            status     = RunStatus.FAILURE,
            exit_code  = 1,
            duration_s = duration_s,
            started_at = started_at,
            stderr     = detail,
            detail     = detail,
            forced     = forced,
        )