"""
app.py — FastAPI application factory.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

import src.config as config
from src.scheduler import Scheduler
from src import notifier

from .routers import tasks, logs

logger = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False)
_DASHBOARD_PATH = Path(__file__).parent / "dashboard.html"


def require_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    expected = config.get("api.token") or ""
    if not expected:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail="api token is not configured")
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="authorization header missing",
                            headers={"WWW-Authenticate": "Bearer"})
    if not secrets.compare_digest(credentials.credentials, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid token")
    return credentials.credentials


def create_app(scheduler: Scheduler, project_root: Path) -> FastAPI:
    disable_docs = bool(config.get("api.disable_docs", False))
    app = FastAPI(
        title       = "py-scheduler",
        description = "Task scheduler with optional REST API",
        version     = "1.0.0",
        docs_url    = None if disable_docs else "/docs",
        redoc_url   = None if disable_docs else "/redoc",
        openapi_url = None if disable_docs else "/openapi.json",
    )

    app.state.scheduler    = scheduler
    app.state.project_root = project_root
    app.state.started_at   = datetime.now()

    app.include_router(tasks.router, prefix="/tasks", tags=["tasks"], dependencies=[Depends(require_token)])
    app.include_router(logs.router,  prefix="/logs",  tags=["logs"],  dependencies=[Depends(require_token)])

    @app.post("/config/reload", tags=["system"], dependencies=[Depends(require_token)])
    def reload_config(request: Request) -> JSONResponse:
        """Re-read config.json from disk and reload task definitions.

        Use this after editing config.json directly (e.g. adding a new task,
        changing ntfy settings). Changes to api.host/port require a restart.
        """
        import src.config as _config  # noqa: PLC0415
        sched: Scheduler = request.app.state.scheduler
        _config.reload()
        sched.reload_tasks()
        tasks = sched.get_tasks()
        return JSONResponse(content={"success": True, "task_count": len(tasks)})

    @app.post("/scheduler/stop", tags=["system"], dependencies=[Depends(require_token)])
    def stop_scheduler(request: Request) -> JSONResponse:
        """Kill the process. Systemd (Restart=always/on-failure) will restart it.

        Sends SIGTERM to self on a short delay so the HTTP response is
        delivered before the process exits.
        """
        import os, signal, threading  # noqa: PLC0415
        def _kill():
            import time; time.sleep(0.3)
            os.kill(os.getpid(), signal.SIGTERM)
        threading.Thread(target=_kill, daemon=True).start()
        return JSONResponse(content={"success": True})

    @app.get("/status", tags=["system"], dependencies=[Depends(require_token)])
    def get_status(request: Request) -> JSONResponse:
        sched: Scheduler = request.app.state.scheduler
        started: datetime = request.app.state.started_at
        uptime_s = (datetime.now() - started).total_seconds()
        task_map = sched.get_tasks()
        enabled  = sum(1 for t in task_map.values() if t.enabled)
        return JSONResponse(content={
            "scheduler_running": sched.is_running,
            "uptime_seconds":    round(uptime_s),
            "task_count":        len(task_map),
            "enabled_count":     enabled,
            "strikes":           notifier.get_strike_state(),
        })

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def dashboard() -> HTMLResponse:
        if _DASHBOARD_PATH.exists():
            return HTMLResponse(_DASHBOARD_PATH.read_text(encoding="utf-8"))
        return HTMLResponse("<h3>dashboard.html not found</h3>", status_code=500)

    return app