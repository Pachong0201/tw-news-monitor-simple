# AI Agent Guide

## Overview

`tw-news-monitor-simple` is a minimal Taiwan news monitoring tool.
Collects news from RSS feeds and HTML pages, deduplicates by URL, saves
to SQLite, and sends a categorized digest.

## Key Design Decisions

- Single `Article` dataclass with `slots=True`.
- Single `articles` table in SQLite with UNIQUE constraint on URL.
- URL normalization: lowercase scheme+host+path, remove fragment + trailing slash.
- Each collector has a self-contained `collect()` method returning up to 20 `Article` instances.
- Digest groups by category (politics, economy, international) and sorts by position, published_at (desc), source_name.
- Notifier factory: ConsoleNotifier by default, falls back from Feishu/Telegram gracefully.
- `--dry-run` uses a temporary SQLite DB that is deleted after the run.

## Architecture

```
main.py → load sources → for each source:
  collector.collect() → list[Article]
  db.article_exists(url) → dedup
  db.save_articles(new)
  build_digest(new) → notifier.send(text)
```

## Adding a Source

Add an entry to `config/sources.yaml` with `id`, `name`, `category`,
`collector` type, and `url`. If a new collector type is needed, add a
class in `app/collectors/` that extends `BaseCollector` and implements
`collect()`, then register it in the `COLLECTOR_MAP` in `main.py`.

## Collector Rules

1. Max 20 items per source.
2. Skip empty titles / URLs.
3. Normalize URL before dedup.
4. Do NOT fetch article body.
5. Do NOT filter by keywords inside collectors. Out-of-scope filtering
   (e.g. social trivia in economy feeds) is handled centrally by
   `app/content_filter.py` + `config/content_filter.yaml` before saving.
6. Do NOT classify as breaking news.
7. Single source failure must not stop others.
8. Set timeout per request.
9. Set reasonable User-Agent.
10. Log clear errors on failure.

## What Is NOT Implemented

- Event aggregation
- Title similarity detection
- News scoring / ranking
- Keyword filtering
- Page snapshots
- LLM classification
- Playwright browser automation
- Complex DB architecture (no events/snapshots/reports tables)
- Pydantic / SQLAlchemy / async frameworks / APScheduler

## What This Version Does NOT Have (Previous Project Issues)

The old `tw-news-monitor` had severe indentation and file-overwriting
problems. This project is rebuilt from scratch without copying any
Python code from the old project.
