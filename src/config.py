"""
config.py — Loads, merges, saves, and exposes config.json.

Usage
-----
    import src.config as config

    config.load()                        # call once at startup
    config.get("api.port")               # dot-notation read
    config.set("api.port", 9000)         # dot-notation write + auto-save
    config.reload()                      # re-read file from disk
"""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _project_root() -> Path:
    """Always resolves to the directory that contains this src/ package."""
    return Path(__file__).resolve().parent.parent


CONFIG_PATH: Path = _project_root() / "config.json"

# ---------------------------------------------------------------------------
# Schema / defaults
# ---------------------------------------------------------------------------

def _defaults() -> dict:
    return {
        "app_logging_level": "INFO",
        "api": {
            "enabled": True,
            "logging_level": "info",
            "disable_docs": False,
            "host": "0.0.0.0",
            "port": 8765,
            "token": "changeme"
        },
        "scheduler": {
            "max_workers": 4,          # max parallel tasks
            "log_dir": "logs",         # relative to project root
            "log_retention_days": 30   # 0 = keep forever
        },
        "ntfy": {
            "enabled": True,
            "topic": "my-topic",  # full topic URL e.g. "https://ntfy.sh/my-topic"
            "token": "",           # Bearer token (optional)
            "strike_limit": 10,    # failures before muting a task
            "strike_reset_hours": 24,  # hours of silence before auto-reset
            "global_cap": 100      # total scheduler notifications before global mute
        },
        "tasks": {
            "example": {
                "name": "Example Task",
                "enabled": False,
                "script": "tasks/example.py",
                "frequency": "daily",
                "times": ["09:00"],
                "timeout": 60,
                "notify_on_success": False,
                "notify_on_failure": True
            }
        }
    }

# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------

_config: dict | None = None

# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into a copy of *base*."""
    result = deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _read_file() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _write_file(data: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp file then replace — avoids a corrupt config if the
    # process is killed mid-write.
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=4)
    tmp.replace(CONFIG_PATH)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load() -> None:
    """Load config.json from disk, deep-merging with defaults.

    Creates config.json with default values if it does not exist.
    Safe to call multiple times — only loads once unless reload() is used.
    """
    global _config

    if _config is not None:
        return  # already loaded

    defaults = _defaults()

    if CONFIG_PATH.exists():
        try:
            file_data = _read_file()
            _config = _deep_merge(defaults, file_data)
            logger.info("config loaded from %s", CONFIG_PATH)
        except (json.JSONDecodeError, OSError) as exc:
            logger.exception("failed to read %s — using defaults. error: %s", CONFIG_PATH, exc)
            _config = defaults
    else:
        logger.info("config.json not found — creating with defaults at %s", CONFIG_PATH)
        _config = defaults
        try:
            _write_file(_config)
        except OSError as exc:
            logger.error("could not write default config: %s", exc)


def reload() -> None:
    """Force a fresh read from disk, discarding any in-memory state."""
    global _config
    _config = None
    load()
    logger.info("config reloaded")


def save() -> None:
    """Persist the current in-memory config to disk."""
    if _config is None:
        logger.warning("save() called before load() — nothing to save")
        return
    try:
        _write_file(_config)
    except OSError as exc:
        logger.error("failed to save config: %s", exc)


def get(key: str | None = None, default: Any = None) -> Any:
    """Read a value using dot-notation, e.g. ``config.get("api.port")``.

    Returns the entire config dict when *key* is ``None``.
    Returns *default* (``None``) when the key path does not exist.
    """
    if _config is None:
        load()

    if key is None:
        return deepcopy(_config)

    node: Any = _config
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def set(key: str, value: Any) -> None:  # noqa: A001
    """Write a value using dot-notation and immediately persist to disk.

    Intermediate dicts are created automatically.
    Example: ``config.set("scheduler.max_workers", 8)``
    """
    if _config is None:
        load()

    parts = key.split(".")
    node = _config
    for part in parts[:-1]:
        if part not in node or not isinstance(node[part], dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = value
    save()


def set_many(updates: dict[str, Any]) -> None:  # noqa: A001
    """Write multiple dot-notation keys and save once.

    Equivalent to calling set() for each key but only writes to disk once.
    Example: set_many({"api.port": 9000, "api.host": "127.0.0.1"})
    """
    if _config is None:
        load()

    for key, value in updates.items():
        parts = key.split(".")
        node = _config
        for part in parts[:-1]:
            if part not in node or not isinstance(node[part], dict):
                node[part] = {}
            node = node[part]
        node[parts[-1]] = value

    save()


def to_dict() -> dict:
    """Return a deep copy of the entire config."""
    if _config is None:
        load()
    return deepcopy(_config)