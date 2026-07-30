tw-news-monitor-simple
=====================

A minimal, runnable Taiwan news monitoring project.

Collect news from 9 sources (中央社, 聯合新聞網, 東森新聞) across 3
categories (politics, economy, international), deduplicate by URL, save
to SQLite, generate a categorized digest, and push to Console / Feishu /
Telegram.


Quick Start
-----------

1. Install dependencies::

   pip install -r requirements.txt

2. Run (collect + save + notify)::

   python -m app.main

   First run fetches all available news. Second run only pushes new
   articles not already in the database.


CLI Commands
------------

``python -m app.main``
    Default mode: collect news, save to SQLite, send digest.

``python -m app.main --dry-run``
    Collect and print digest to console without saving to DB or sending
    real notifications. Uses a temporary SQLite database that is deleted
    after the run.

``python -m app.main --test-notify``
    Send a test notification via the configured notifier.

``python -m app.main --db-stats``
    Show database statistics: total article count, counts by source and
    by category.


Configuration
-------------

Copy ``.env.example`` to ``.env`` and set the following:

``NOTIFIER``
    ``console`` (default), ``feishu``, or ``telegram``.
``FEISHU_WEBHOOK_URL``
    Feishu webhook URL (required for Feishu).
``TELEGRAM_BOT_TOKEN``
    Telegram bot token (required for Telegram).
``TELEGRAM_CHAT_ID``
    Target chat ID (required for Telegram).

If Feishu / Telegram config is missing, the program falls back to
console output gracefully.

Sources are configured in ``config/sources.yaml``. 9 sources are
pre-configured. Add or modify entries as needed.


Project Structure
-----------------

::

   tw-news-monitor-simple/
   ├── app/
   │   ├── __init__.py
   │   ├── models.py            # Article dataclass
   │   ├── database.py          # SQLite storage
   │   ├── digest.py            # Digest builder
   │   ├── notifier.py          # Console / Feishu / Telegram
   │   ├── main.py              # CLI entry point
   │   └── collectors/
   │       ├── __init__.py
   │       ├── base.py          # Abstract base + URL normalization
   │       ├── rss.py           # RSS/Atom feed collector
   │       ├── udn.py           # UDN HTML page collector
   │       └── ebc.py           # EBC HTML page collector
   ├── config/
   │   └── sources.yaml         # Source definitions
   ├── data/                    # SQLite DB (auto-created)
   ├── tests/
   │   ├── __init__.py
   │   ├── fixtures/            # Sample HTML/XML fixtures
   │   ├── test_database.py
   │   ├── test_collectors.py
   │   └── test_digest.py
   ├── .env.example
   ├── .gitignore
   ├── requirements.txt
   └── README.md


Running Tests
-------------

::

   python -m pytest tests/ -v

Tests use local fixture files and do not access the internet.


Tech Stack
----------

- Python 3.12+ (standard library sqlite3)
- httpx, feedparser, beautifulsoup4, PyYAML, python-dotenv, pytest

No Pydantic, SQLAlchemy, async frameworks, APScheduler, Docker, Redis,
PostgreSQL, or Playwright.


Windows Task Scheduler Deployment
-------------------------------

The scheduled task runs every 30 minutes automatically.

Install scheduled task (requires PowerShell):
  Double-click: install_task.bat
  Or command:   powershell.exe -NoProfile -ExecutionPolicy Bypass -File install_scheduled_task.ps1

Check status:  Double-click task_status.bat
Run now:      Double-click run_task_now.bat
Uninstall:    Double-click uninstall_task.bat

Configuration
  - Task name: Taiwan News Monitor
  - Entry: run_monitor.bat (runs `py -m app.main`)
  - Interval: every 30 minutes
  - First run: ~1 minute after installation
  - Single-instance protection: both Task Scheduler IgnoreNew + in-app lock

Notes
  - Collection requires the computer to be on and connected.
  - The Python logging module writes structured logs to data/monitor.log.
  - New articles trigger automatic Word generation and Feishu delivery.
  - No news = no Word file generated (avoids empty attachments).
  - Do NOT delete data/news.db. It contains the article history and dedup state.

License
-------

MIT
