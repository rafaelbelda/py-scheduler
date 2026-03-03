"""
main.py — Application entrypoint.

Usage
-----
    python main.py                  # start with API (if enabled in config)
    python main.py --no-api         # force-disable API regardless of config
    python main.py --reload-config  # reload config file then start

The scheduler always starts. The FastAPI server starts only when
config.api.enabled is True (default) and --no-api is not passed.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
from pathlib import Path

# Project root is the directory containing this file.
PROJECT_ROOT = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Bootstrap: config must load before anything else is imported that reads it.
# ---------------------------------------------------------------------------
import src.config as config

config.load()

# ---------------------------------------------------------------------------
# Logging setup (reads level from config)
# ---------------------------------------------------------------------------

def _setup_logging() -> None:
    level_name = (config.get("app_logging_level") or "INFO").upper()
    level      = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level   = level,
        format  = "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt = "%Y-%m-%d %H:%M:%S",
        stream  = sys.stdout,
    )

_setup_logging()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Late imports (after config is loaded)
# ---------------------------------------------------------------------------

from src.scheduler import Scheduler  # noqa: E402


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="py-scheduler")
    parser.add_argument(
        "--no-api",
        action = "store_true",
        help   = "Disable the FastAPI server even if enabled in config",
    )
    args = parser.parse_args()

    api_enabled = config.get("api.enabled", True) and not args.no_api

    logger.info("starting py-scheduler (api=%s)", api_enabled)

    # ---- Scheduler -------------------------------------------------------
    scheduler = Scheduler(project_root=PROJECT_ROOT)
    scheduler.start()

    # ---- API (optional) --------------------------------------------------
    if api_enabled:
        _start_api(scheduler)
    else:
        logger.info("api disabled — running scheduler only")
        # Block main thread so the daemon scheduler thread stays alive.
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            logger.info("keyboard interrupt received — shutting down")
            scheduler.stop()


def _start_api(scheduler: Scheduler) -> None:
    try:
        import uvicorn  # noqa: PLC0415
    except ImportError:
        logger.error(
            "uvicorn is not installed. install it with: pip install uvicorn\n"
            "or start without the api using: python main.py --no-api"
        )
        sys.exit(1)

    from src.api.app import create_app  # noqa: PLC0415

    app  = create_app(scheduler, PROJECT_ROOT)
    host = config.get("api.host", "0.0.0.0")
    port = int(config.get("api.port", 8765))

    logger.info("api listening on %s:%d", host, port)

    try:
        uvicorn_level = (config.get("api.logging_level") or "info").lower()
        uvicorn.run(app, host=host, port=port, log_level=uvicorn_level)
    except KeyboardInterrupt:
        logger.info("keyboard interrupt received — shutting down")
    finally:
        scheduler.stop()


if __name__ == "__main__":
    main()