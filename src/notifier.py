"""
notifier.py — ntfy-based notification system with per-task strike suppression.

Two entry points
----------------
notify_result(result)
    Called by the scheduler after every task run. Handles error formatting,
    strike counting, and suppression automatically. Never called for forced runs.

send(title, message, ...)
    Called directly by task scripts for custom notifications (e.g. "new item
    found"). Bypasses the strike system entirely — these are intentional signals.

Strike system (notify_result only)
-----------------------------------
Each task has an independent strike counter that increments on every failure or
timeout notification sent. Once strikes reach ``strike_limit``, a final "now
muted" notification is sent and that task is silenced until the 24h auto-reset.

Strikes reset ONLY when more than ``strike_reset_hours`` hours have passed since
the last failure. A successful run does NOT reset strikes — a flapping task
(fail→success→fail) would otherwise never mute.

Global cap
----------
A process-lifetime counter across all tasks. When it reaches ``global_cap``, one
final warning is sent and all scheduler-triggered notifications stop until restart.
Direct send() calls are NOT counted toward the global cap.

Config (under "ntfy")
---------------------
    enabled            bool    master switch (default false)
    topic              str     ntfy topic name, e.g. "my-topic" → posts to https://ntfy.sh/my-topic
    token              str     optional Bearer token for auth
    strike_limit       int     failures before muting a task (default 10)
    strike_reset_hours float   hours of silence before auto-reset (default 24)
    global_cap         int     total scheduler notifications before global mute (default 100)
"""

from __future__ import annotations

import unicodedata
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .models import RunResult, RunStatus

logger = logging.getLogger(__name__)

def _encode_title(title: str, use_latin1_encoding: bool) -> str:
    """Return a header-safe title string."""

    # normalize common unicode dashes -- LLMs love that shit man
    title = title.replace("–", "-").replace("—", "-").replace("−", "-")

    if use_latin1_encoding:
        try:
            return title.encode("utf-8").decode("latin-1")
        except Exception:
            pass

    clean_title = unicodedata.normalize("NFKD", title)
    return clean_title.encode("latin-1", "replace").decode("latin-1")

# ---------------------------------------------------------------------------
# In-memory strike state
# ---------------------------------------------------------------------------

@dataclass
class _TaskStrikeEntry:
    strikes: int = 0
    muted: bool = False
    last_failure_ts: float = 0.0


_state_lock          = threading.Lock()
_task_strikes: dict[str, _TaskStrikeEntry] = {}
_global_sent: int    = 0
_global_muted: bool  = False


def get_strike_state() -> dict[str, Any]:
    """Return a snapshot of current strike state (for API/debug inspection)."""
    with _state_lock:
        return {
            "global_sent":  _global_sent,
            "global_muted": _global_muted,
            "tasks": {
                k: {"strikes": v.strikes, "muted": v.muted}
                for k, v in _task_strikes.items()
            },
        }


def reset_strikes(task_key: str | None = None) -> None:
    """Manually reset strike state.

    Args:
        task_key: Reset only this task. Pass None to reset everything
                  including the global counter.
    """
    global _global_sent, _global_muted
    with _state_lock:
        if task_key is None:
            _task_strikes.clear()
            _global_sent  = 0
            _global_muted = False
            logger.info("notifier: all strike state reset")
        else:
            _task_strikes.pop(task_key, None)
            logger.info("notifier: strike state reset for task '%s'", task_key)


# ---------------------------------------------------------------------------
# Low-level ntfy sender
# ---------------------------------------------------------------------------

def send(
    title: str,
    message: str,
    topic: str | None = None,
    *,
    priority: str = "default",
    click_url: str | None = None,
    emojis: list[str] | None = None,
    use_latin1_encoding: bool = False,
) -> bool:
    """Send a single ntfy notification. Returns True on success.

    Called directly by task scripts for custom alerts. Bypasses the strike
    system and global cap — use deliberately.

    Args:
        title:               Notification title (shown in bold).
        message:             Notification body.
        priority:            ntfy priority: min, low, default, high, max/urgent.
        click_url:           URL to open when the notification is tapped.
        emojis:              List of emoji tag names, e.g. ["cat", "warning"].
        use_latin1_encoding: Encode title as latin-1 (workaround for some
                             ntfy Android clients that mishandle UTF-8 titles).
    """
    import src.config as config  # noqa: PLC0415

    cfg = config.get("ntfy") or {}

    if not cfg.get("enabled", False):
        return False

    topic = (topic if topic is not None else cfg.get("topic")) or ""
    topic = topic.strip()
    
    if not topic:
        logger.warning("notifier: ntfy enabled but 'topic' is empty")
        return False
    
    url = "https://ntfy.sh/" + topic

    return _send_raw(
        url               = url,
        token             = cfg.get("token") or "",
        title             = title,
        message           = message,
        priority          = priority,
        click_url         = click_url,
        emojis            = emojis,
        use_latin1_encoding = use_latin1_encoding,
    )


def _send_raw(
    url: str,
    token: str,
    title: str,
    message: str,
    priority: str,
    click_url: str | None,
    emojis: list[str] | None,
    use_latin1_encoding: bool,
) -> bool:
    """Execute the HTTP request to ntfy. Returns True on HTTP success."""
    headers: dict[str, str] = {
        "Priority": priority,
        "Content-Type": "text/plain; charset=utf-8",
    }

    headers["Title"] = _encode_title(title, use_latin1_encoding)

    if token:
        headers["Authorization"] = f"Bearer {token}"

    if emojis:
        headers["Tags"] = ",".join(emojis)

    if click_url:
        headers["Click"] = click_url

    body = message.encode("utf-8")
    req  = urllib.request.Request(url, data=body, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            logger.debug("ntfy delivered: %d — title='%s'", resp.status, title)
            return True
    except urllib.error.HTTPError as exc:
        logger.error("ntfy HTTP error %d %s — title='%s'", exc.code, exc.reason, title)
    except urllib.error.URLError as exc:
        logger.error("ntfy URL error: %s — title='%s'", exc.reason, title)
    except Exception as exc:  # noqa: BLE001
        logger.error("ntfy unexpected error: %s — title='%s'", exc, title)
    return False


# ---------------------------------------------------------------------------
# Scheduler-facing entry point
# ---------------------------------------------------------------------------

def notify_result(result: RunResult) -> None:
    """Send a notification for a scheduler task run, with strike suppression.

    Never raises. Silently skips when:
      - result.forced is True
      - ntfy is disabled or topic is empty
      - the task's notify flag is False for this outcome
      - the task is muted
      - the global cap is reached
    """
    global _global_sent, _global_muted

    if result.forced:
        return

    import src.config as config  # noqa: PLC0415

    cfg = config.get("ntfy") or {}

    if not cfg.get("enabled", False):
        return

    topic = (cfg.get("topic") or "").strip()
    if not topic:
        logger.warning("notifier: ntfy enabled but 'topic' is empty — skipping notify_result")
        return
    url = "https://ntfy.sh/" + topic

    token = cfg.get("token") or ""

    # Per-task notify flags
    task_cfg       = (config.get("tasks") or {}).get(result.task_key, {})
    notify_success = bool(task_cfg.get("notify_on_success", False))
    notify_failure = bool(task_cfg.get("notify_on_failure", True))

    is_failure = result.status in (RunStatus.FAILURE, RunStatus.TIMEOUT)
    is_success = result.status == RunStatus.SUCCESS

    if is_failure and not notify_failure:
        return
    if is_success and not notify_success:
        return

    strike_limit       = int(cfg.get("strike_limit", 10))
    strike_reset_hours = float(cfg.get("strike_reset_hours", 24))
    global_cap         = int(cfg.get("global_cap", 100))

    # ------------------------------------------------------------------
    # Determine what (if anything) to send — hold the lock only for state
    # reads/writes, not for the HTTP call itself.
    # ------------------------------------------------------------------
    send_kwargs: dict | None = None
    fire_cap_warning = False

    with _state_lock:

        # Success path
        if is_success:
            if _global_muted or _global_sent >= global_cap:
                if not _global_muted:
                    _global_muted = True
                    fire_cap_warning = True
            else:
                title   = f"SUCCESS: Task '{result.task_name}'"
                message = f"Task '{result.task_name}' completed in {result.duration_s:.2f}s."
                if result.stdout:
                    message += f"\nstdout: {result.stdout[:80]}{'... (truncated)' if len(result.stdout) > 80 else ''}"
                send_kwargs = dict(url=url, token=token, title=title, message=message,
                                   priority="low", click_url=None, emojis=None,
                                   use_latin1_encoding=False)

        else:
            # Failure / timeout path
            if result.task_key not in _task_strikes:
                _task_strikes[result.task_key] = _TaskStrikeEntry()
            entry = _task_strikes[result.task_key]

            # Auto-reset if last failure was more than strike_reset_hours ago.
            if entry.strikes > 0 and (time.time() - entry.last_failure_ts) > strike_reset_hours * 3600:
                logger.info(
                    "notifier: task '%s' strike auto-reset after %.1fh of silence",
                    result.task_key, strike_reset_hours,
                )
                entry.strikes         = 0
                entry.muted           = False
                entry.last_failure_ts = 0.0

            if entry.muted:
                logger.debug(
                    "notifier: task '%s' is muted (%d strikes) — suppressing",
                    result.task_key, entry.strikes,
                )
            elif _global_muted:
                pass
            elif _global_sent >= global_cap:
                _global_muted    = True
                fire_cap_warning = True
            else:
                entry.strikes        += 1
                entry.last_failure_ts = time.time()
                current_strike        = entry.strikes

                base_title = f"ERROR: Task '{result.task_name}'"
                title      = f"{base_title} ({current_strike}/{strike_limit})" if current_strike > 1 else base_title

                is_mute_strike = current_strike >= strike_limit
                if is_mute_strike:
                    entry.muted = True
                    message  = (
                        f"Task '{result.task_name}' has failed {current_strike} time(s) in a row.\n"
                        f"Further failure notifications are now suppressed until "
                        f"{strike_reset_hours:.0f}h of silence passes."
                    )
                    priority = "high"
                    logger.warning(
                        "notifier: task '%s' hit strike limit (%d) — muting",
                        result.task_key, strike_limit,
                    )
                else:
                    _STDERR_CAP = 500
                    if result.status == RunStatus.TIMEOUT:
                        detail_line = f"rc=timeout in {result.duration_s:.1f}s"
                    else:
                        detail_line = f"rc={result.exit_code} in {result.duration_s:.2f}s"

                    body_lines: list[str] = [detail_line]
                    if result.stderr:
                        total = len(result.stderr)
                        if total > _STDERR_CAP:
                            body_lines.append(
                                result.stderr[:_STDERR_CAP]
                                + f"\n... ({total}B total, truncated to {_STDERR_CAP})"
                            )
                        else:
                            body_lines.append(result.stderr)

                    message  = "\n".join(body_lines)
                    priority = "high"

                send_kwargs = dict(url=url, token=token, title=title, message=message,
                                   priority=priority, click_url=None, emojis=None,
                                   use_latin1_encoding=False)

    # ------------------------------------------------------------------
    # HTTP calls happen outside the lock — never block other threads.
    # ------------------------------------------------------------------
    if fire_cap_warning:
        _fire_global_cap_warning(url, token, global_cap)
        return

    if send_kwargs:
        if _send_raw(**send_kwargs):
            with _state_lock:
                _global_sent += 1


def _fire_global_cap_warning(url: str, token: str, cap: int) -> None:
    """Send the one-time global cap warning. Called while _state_lock is held."""
    global _global_sent
    title   = "Scheduler: notifications muted"
    message = (
        f"py-scheduler has sent {cap} notifications since startup. "
        "Further error notifications are suppressed until the process restarts."
    )
    logger.warning("notifier: global cap (%d) reached — sending final warning", cap)
    if _send_raw(url, token, title, message, priority="high",
                 click_url=None, emojis=None, use_latin1_encoding=False):
        _global_sent += 1