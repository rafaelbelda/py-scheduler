"""
routers/tasks.py — Task management endpoints.

All routes are mounted under /tasks by app.py.
Auth is handled by the dependency injected in app.py — no duplication here.

Endpoints
---------
GET  /tasks                   list all tasks and their status
GET  /tasks/{key}             get one task definition
POST /tasks/{key}/run         trigger immediate execution
POST /tasks/{key}/enable      enable a task
POST /tasks/{key}/disable     disable a task
POST /tasks/reload            reload task definitions from config
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

import src.config as config
from src import notifier

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _task_to_dict(task) -> dict:
    return {
        "key":              task.key,
        "name":             task.name,
        "enabled":          task.enabled,
        "script":           task.script,
        "frequency":        task.frequency.value,
        "times":            task.times,
        "days_of_week":     task.days_of_week,
        "day_of_month":     task.day_of_month,
        "month_day":        task.month_day,
        "specific_date":    task.specific_date,
        "interval_unit":    task.interval_unit.value if task.interval_unit else None,
        "interval_value":   task.interval_value,
        "timeout":          task.timeout,
        "notify_on_success": task.notify_on_success,
        "notify_on_failure": task.notify_on_failure,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("")
def list_tasks(request: Request) -> JSONResponse:
    """Return all configured tasks."""
    scheduler = request.app.state.scheduler
    tasks = scheduler.get_tasks()
    return JSONResponse(content={
        "count": len(tasks),
        "tasks": {k: _task_to_dict(v) for k, v in tasks.items()},
    })


@router.get("/reload")
def reload_tasks_get(request: Request) -> JSONResponse:
    """Alias of POST /tasks/reload for convenience."""
    return reload_tasks(request)

@router.get("/strikes")
def get_strikes(request: Request) -> JSONResponse:
    """Return current in-memory strike state for all tasks."""
    return JSONResponse(content=notifier.get_strike_state())

@router.get("/{key}")
def get_task(key: str, request: Request) -> JSONResponse:
    """Return a single task definition by key."""
    scheduler = request.app.state.scheduler
    tasks = scheduler.get_tasks()

    if key not in tasks:
        raise HTTPException(status_code=404, detail=f"task '{key}' not found")

    return JSONResponse(content=_task_to_dict(tasks[key]))


@router.post("/{key}/run")
def run_task(key: str, request: Request) -> JSONResponse:
    """Trigger immediate (forced) execution of a task.

    Runs synchronously from the API caller's perspective — blocks until done.
    """
    scheduler = request.app.state.scheduler

    try:
        future = scheduler.run_now(key)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"task '{key}' not found")

    try:
        result = future.result(timeout=3600)  # generous ceiling; task has its own timeout
    except Exception as exc:
        logger.exception("error awaiting forced run of task '%s'", key)
        raise HTTPException(status_code=500, detail=str(exc))

    status_code = 200 if result.ok else 500
    return JSONResponse(
        content={
            "success":    result.ok,
            "status":     result.status.value,
            "exit_code":  result.exit_code,
            "duration_s": round(result.duration_s, 3),
            "detail":     result.detail,
            "stdout":     result.stdout,
            "stderr":     result.stderr,
        },
        status_code=status_code,
    )


@router.post("/{key}/enable")
def enable_task(key: str, request: Request) -> JSONResponse:
    """Enable a task (persists to config.json)."""
    scheduler = request.app.state.scheduler
    tasks = scheduler.get_tasks()

    if key not in tasks:
        raise HTTPException(status_code=404, detail=f"task '{key}' not found")

    config.set(f"tasks.{key}.enabled", True)
    scheduler.reload_tasks()
    return JSONResponse(content={"success": True, "task": key, "enabled": True})


@router.post("/{key}/disable")
def disable_task(key: str, request: Request) -> JSONResponse:
    """Disable a task (persists to config.json)."""
    scheduler = request.app.state.scheduler
    tasks = scheduler.get_tasks()

    if key not in tasks:
        raise HTTPException(status_code=404, detail=f"task '{key}' not found")

    config.set(f"tasks.{key}.enabled", False)
    scheduler.reload_tasks()
    return JSONResponse(content={"success": True, "task": key, "enabled": False})



@router.post("/reload")
def reload_tasks(request: Request) -> JSONResponse:
    """Reload task definitions from the in-memory config.

    To pick up changes written directly to config.json (e.g. new tasks),
    call POST /config/reload instead — it re-reads disk and calls this automatically.
    """
    scheduler = request.app.state.scheduler
    scheduler.reload_tasks()
    tasks = scheduler.get_tasks()
    return JSONResponse(content={"success": True, "task_count": len(tasks)})


# ---------------------------------------------------------------------------
# Strike / notification suppression endpoints
# ---------------------------------------------------------------------------


@router.post("/strikes/reset")
def reset_all_strikes(request: Request) -> JSONResponse:
    """Reset strike counters and global cap for ALL tasks.

    Use this after fixing a recurring error to immediately re-enable
    notifications without restarting the process.
    """
    notifier.reset_strikes()
    return JSONResponse(content={"success": True, "reset": "all"})


@router.post("/{key}/strikes/reset")
def reset_task_strikes(key: str, request: Request) -> JSONResponse:
    """Reset strike counter for a single task."""
    scheduler = request.app.state.scheduler
    tasks = scheduler.get_tasks()
    if key not in tasks:
        raise HTTPException(status_code=404, detail=f"task '{key}' not found")
    notifier.reset_strikes(key)
    return JSONResponse(content={"success": True, "reset": key})


@router.patch("/{key}")
def update_task(key: str, request: Request, body: dict) -> JSONResponse:
    """Update one or more fields of a task definition and persist to config.json.

    Only the fields present in the request body are updated.
    Call POST /tasks/reload after to apply changes to the running scheduler.
    """
    scheduler = request.app.state.scheduler
    tasks = scheduler.get_tasks()
    if key not in tasks:
        raise HTTPException(status_code=404, detail=f"task '{key}' not found")

    # Allowed editable fields
    EDITABLE = {
        "name", "enabled", "script", "frequency", "times", "days_of_week",
        "day_of_month", "month_day", "specific_date", "interval_unit",
        "interval_value", "timeout", "notify_on_success", "notify_on_failure",
    }

    rejected = [k for k in body if k not in EDITABLE]
    if rejected:
        raise HTTPException(status_code=422, detail=f"non-editable fields: {rejected}")

    config.set_many({f"tasks.{key}.{field_name}": value for field_name, value in body.items()})
    scheduler.reload_tasks()
    return JSONResponse(content={"success": True, "task": key, "updated": list(body.keys())})