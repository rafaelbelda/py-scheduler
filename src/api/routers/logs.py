"""
routers/logs.py — Log retrieval endpoints.

All routes are mounted under /logs by app.py.

Endpoints
---------
GET /logs/{date}                     all entries for YYYY-MM-DD
GET /logs/{date}?task_key=backup     filtered by task
GET /logs/range?start=…&end=…        range query (optional task_key filter)
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

import src.task_logger as task_logger

logger = logging.getLogger(__name__)

router = APIRouter()


def _validate_date(date_str: str, param_name: str = "date") -> str:
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"'{param_name}' must be in YYYY-MM-DD format, got '{date_str}'",
        )
    return date_str


@router.get("/{date}")
def get_logs_for_date(
    date: str,
    request: Request,
    task_key: str | None = Query(default=None, description="Filter by task key"),
) -> JSONResponse:
    """Return all log entries for a given date.

    Optionally filtered by *task_key*.
    """
    _validate_date(date)
    project_root = request.app.state.project_root

    entries = task_logger.read(date, project_root)

    if task_key:
        entries = [e for e in entries if e.get("task_key") == task_key]

    return JSONResponse(content={"date": date, "count": len(entries), "entries": entries})


@router.get("/range/query")
def get_logs_range(
    request: Request,
    start:    str       = Query(..., description="Start date YYYY-MM-DD"),
    end:      str       = Query(..., description="End date YYYY-MM-DD"),
    task_key: str | None = Query(default=None, description="Filter by task key"),
) -> JSONResponse:
    """Return log entries across an inclusive date range."""
    _validate_date(start, "start")
    _validate_date(end,   "end")

    if start > end:
        raise HTTPException(status_code=422, detail="'start' must be <= 'end'")

    project_root = request.app.state.project_root
    entries = task_logger.read_range(start, end, project_root, task_key=task_key)

    return JSONResponse(content={
        "start":    start,
        "end":      end,
        "task_key": task_key,
        "count":    len(entries),
        "entries":  entries,
    })


@router.get("/stats/summary")
def get_stats_summary(
    request: Request,
    days: int = Query(default=7, ge=1, le=90, description="How many days back to analyse"),
) -> JSONResponse:
    """Return per-task error rates and run counts for the last N days.

    Used by the dashboard to render error-rate badges and sparklines.
    """
    project_root = request.app.state.project_root

    end   = date.today()
    start = end - timedelta(days=days - 1)

    entries = task_logger.read_range(start, end, project_root)

    # Aggregate per task
    stats: dict[str, dict] = {}
    for e in entries:
        k = e.get("task_key", "unknown")
        if k not in stats:
            stats[k] = {"total": 0, "success": 0, "failure": 0, "timeout": 0}
        stats[k]["total"] += 1
        status = e.get("status", "")
        if status in stats[k]:
            stats[k][status] += 1

    # Compute error rate
    for k, s in stats.items():
        s["error_rate"] = round(
            (s["failure"] + s["timeout"]) / s["total"] if s["total"] else 0, 3
        )

    return JSONResponse(content={
        "days":  days,
        "start": start.isoformat(),
        "end":   end.isoformat(),
        "tasks": stats,
    })