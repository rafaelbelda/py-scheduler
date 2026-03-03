# py-scheduler

A lightweight Python task scheduler with a web dashboard and optional REST API.

Write tasks as plain Python scripts. The scheduler runs them on your defined schedule.
No cron syntax, no external services, no dependencies beyond the standard library (plus FastAPI/uvicorn if you want the API and dashboard).

A special thanks to Sonnet 4.6 for most of this.

## Features

- **Seven schedule types:** daily, weekly, monthly, yearly, interval, specific date, once
- **Interval scheduling** is midnight-anchored — "every 6 hours" always fires at 00:00, 06:00, 12:00, 18:00 regardless of when the process started
- **Parallel execution** with a configurable worker limit
- **Structured JSONL logs** — one file per day, queryable by date range and task
- **Automatic log pruning** — configurable retention period
- **ntfy notifications** with per-task strike suppression — mutes a flapping task after N consecutive failures, auto-resets after a configurable silence window
- **Web dashboard** — overview, task management, log viewer, strike inspector; accessible at `GET /`
- **REST API** — list, run, enable, disable, and edit tasks without touching config.json
- **`once` frequency** — auto-disables a task after its first successful run
- **Config hot-reload** — `POST /config/reload` picks up disk changes instantly

## Quickstart

```bash
git clone https://github.com/your-username/py-scheduler
cd py-scheduler

pip install -r requirements.txt

cp config.example.json config.json
# edit config.json — at minimum change api.token

python main.py
```

Open `http://localhost:8765` for the dashboard.

To run without the API or dashboard:

```bash
python main.py --no-api
```

## Project Structure

```
py-scheduler/
├── main.py                   # entrypoint
├── config.json               # your config (gitignored)
├── config.example.json       # committed template
├── requirements.txt
├── tasks/                    # put your task scripts here
│   └── example.py
├── logs/                     # JSONL run logs (gitignored)
└── src/
    ├── scheduler.py          # tick loop, due-time logic
    ├── runner.py             # subprocess execution
    ├── task_parser.py        # config → TaskDefinition validation
    ├── task_logger.py        # JSONL log writer/reader
    ├── notifier.py           # ntfy notifications + strike system
    ├── config.py             # JSON config loader
    ├── models.py             # dataclasses (TaskDefinition, RunResult)
    └── api/
        ├── app.py            # FastAPI app factory
        ├── dashboard.html    # single-file web dashboard
        └── routers/
            ├── tasks.py      # /tasks endpoints
            └── logs.py       # /logs endpoints
```

## Writing a Task

Tasks are plain Python scripts. The scheduler runs them as a module (`python -m tasks.your_task`) so relative imports within the `tasks/` package work correctly.

```python
# tasks/my_task.py
import sys

def main():
    print("task completed")
    sys.exit(0)   # 0 = success, anything else = failure

if __name__ == "__main__":
    main()
```

Exit code `0` = success. Anything else = failure. stdout and stderr are both captured and stored in the log.

### Sending custom notifications from a task

Tasks can send ntfy notifications directly, bypassing the strike system:

```python
from src.notifier import send

send(
    title   = "New item found",
    message = "Something interesting happened",
    priority  = "high",
    click_url = "https://example.com",
    topic = "custom-topic"
    emojis    = ["tada"],
)
```

### Suggested workflow for managing tasks

Keep your task scripts in a separate private git repository checked out into `tasks/`:

```bash
git clone https://github.com/you/my-tasks tasks/
```

To deploy changes: push to the tasks repo, then pull on the server. You can automate this with a self-updating task:

```python
# tasks/self_update.py
import subprocess, sys

r = subprocess.run(["git", "-C", "tasks", "pull"], capture_output=True, text=True)
if r.returncode != 0:
    print(r.stderr, file=sys.stderr)
    sys.exit(1)
print(r.stdout.strip())
```

After adding new tasks to `config.json`, hit **reload config** in the dashboard or call `POST /config/reload`.

## Configuration

`config.json` is created automatically with defaults on first run. Edit it directly or use the dashboard.

### Top-level keys

| Key | Default | Description |
|---|---|---|
| `api.enabled` | `true` | Start the FastAPI server and dashboard |
| `api.host` | `"0.0.0.0"` | Bind address |
| `api.port` | `8765` | Port |
| `api.token` | `"changeme"` | Bearer token — change this |
| `api.logging_level` | `"info"` | Uvicorn access log level |
| `api.disable_docs` | `false` | Set `true` to hide `/docs`, `/redoc`, `/openapi.json` |
| `app_logging_level` | `"INFO"` | Python/scheduler log level |
| `scheduler.max_workers` | `4` | Max parallel tasks |
| `scheduler.log_dir` | `"logs"` | Log directory (relative to project root) |
| `scheduler.log_retention_days` | `30` | Days to keep logs; `0` = keep forever |
| `ntfy.enabled` | `true` | Enable ntfy push notifications |
| `ntfy.topic` | `"my-topic"` | Your ntfy topic |
| `ntfy.token` | `""` | ntfy Bearer token (optional) |
| `ntfy.strike_limit` | `10` | Consecutive failures before muting a task |
| `ntfy.strike_reset_hours` | `24` | Hours of silence before strike auto-reset |
| `ntfy.global_cap` | `100` | Total notifications before global mute (resets on restart) |

### Task fields

| Field | Required | Description |
|---|---|---|
| `name` | no | Human-readable label (defaults to key) |
| `enabled` | no | Default `true` |
| `script` | **yes** | Path relative to project root, e.g. `"tasks/backup.py"` |
| `frequency` | **yes** | See table below |
| `timeout` | no | Seconds before the task is killed; default `300` |
| `notify_on_success` | no | Default `false` |
| `notify_on_failure` | no | Default `true` |

### Frequency types

| `frequency` | Required fields |
|---|---|
| `daily` | `times` |
| `weekly` | `days_of_week`, `times` |
| `monthly` | `day_of_month` (1–31), `times` |
| `yearly` | `month_day` (MM-DD), `times` |
| `interval` | `interval_unit` (minutes/hours/days), `interval_value` |
| `specific` | `specific_date` (YYYY-MM-DD), `times` |
| `once` | `specific_date` (YYYY-MM-DD), `times` — runs once, then auto-disables |

`times` is a list of `"HH:MM"` strings (24-hour). Multiple times are supported.

`days_of_week` accepts names (`"monday"`) or integers (0=Monday … 6=Sunday).

### Example task config

```json
"tasks": {
    "backup": {
        "name": "Daily Backup",
        "script": "tasks/backup.py",
        "frequency": "daily",
        "times": ["02:00"],
        "timeout": 600,
        "notify_on_failure": true
    },
    "report": {
        "name": "Weekly Report",
        "script": "tasks/report.py",
        "frequency": "weekly",
        "days_of_week": ["monday"],
        "times": ["09:00"],
        "notify_on_success": true
    },
    "sync": {
        "name": "Hourly Sync",
        "script": "tasks/sync.py",
        "frequency": "interval",
        "interval_unit": "hours",
        "interval_value": 1
    }
}
```

## REST API

All endpoints require `Authorization: Bearer <token>`.

```
GET    /                              web dashboard
GET    /status                        scheduler health, uptime, strike state

GET    /tasks                         list all tasks
GET    /tasks/{key}                   get one task
POST   /tasks/{key}/run               trigger immediately (blocks until done)
POST   /tasks/{key}/enable            enable (persists to config.json)
POST   /tasks/{key}/disable           disable (persists to config.json)
PATCH  /tasks/{key}                   edit task fields (persists to config.json)
POST   /tasks/reload                  reload from in-memory config
GET    /tasks/strikes                 inspect strike state
POST   /tasks/strikes/reset           reset all strikes
POST   /tasks/{key}/strikes/reset     reset one task's strikes

GET    /logs/{date}                   entries for YYYY-MM-DD
GET    /logs/{date}?task_key=x        filtered by task
GET    /logs/range/query?start=…&end=…[&task_key=x]
GET    /logs/stats/summary?days=7     per-task error rates and run counts

POST   /config/reload                 re-read config.json from disk + reload tasks
```

Interactive docs at `http://localhost:8765/docs`.

## Log Format

Each line in `logs/YYYY-MM-DD.jsonl` is one task run:

```json
{
    "ts":         "2025-01-15T07:00:03.124",
    "task_key":   "backup",
    "task_name":  "Daily Backup",
    "status":     "success",
    "exit_code":  0,
    "duration_s": 4.231,
    "forced":     false,
    "stdout":     "backed up 1.2GB",
    "stderr":     null,
    "detail":     "exited 0 in 4.23s | stdout: backed up 1.2GB"
}
```

`status` is one of `success`, `failure`, `timeout`, `skipped`.
`forced` is `true` when the run was triggered manually via the API or dashboard.
Timestamps are in UTC.

## ntfy Strike System

To prevent a broken task from spamming your phone:

- Each task has an independent failure counter (strikes)
- At `strike_limit` (default 10), a final warning is sent and the task is silenced
- Strikes reset automatically after `strike_reset_hours` (default 24h) of no failures
- Strikes do **not** reset on success — a task that alternates fail/success would otherwise never mute
- A global cap across all tasks stops all scheduler notifications if something goes seriously wrong
- Strike state is in-memory and resets on process restart — no task is permanently silenced
- Use the dashboard Strikes tab or `POST /tasks/{key}/strikes/reset` to reset manually

## License

MIT