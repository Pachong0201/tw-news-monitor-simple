"""Backfill durable news topics without recollecting or delivering news."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import yaml

from .database import Database
from .election2026.classifier import classify_articles as classify_election_articles
from .election2026.config import load_election_config, load_entities
from .settings import get_settings, load_environment


def _load_sources(path: Path) -> dict[str, dict]:
    with path.open(encoding="utf-8-sig") as stream:
        payload = yaml.safe_load(stream) or {}
    rows = payload.get("sources") if isinstance(payload, dict) else []
    return {
        row["id"]: row
        for row in rows
        if isinstance(row, dict) and row.get("id")
    }


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def backfill_topics(
    db: Database,
    *,
    sources: dict[str, dict],
    election_config: dict | None,
    election_entities: dict | None,
    batch_size: int = 500,
    since: datetime | None = None,
    until: datetime | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    scanned = 0
    election_rows = 0
    military_rows = 0
    written = 0
    batch = []

    def flush(rows: list[dict]) -> None:
        nonlocal written
        if not rows or dry_run:
            return
        written += db.save_topics(rows)

    for article in db.iter_articles(
        batch_size=max(1, min(batch_size, 5000)), since=since, until=until
    ):
        scanned += 1
        batch.append(article)
        if len(batch) < batch_size:
            continue
        annotations = classify_election_articles(
            batch, election_config or {"enabled": False}, election_entities or {}
        )
        rows = [
            {
                "url": url,
                "topic": "election_2026_local",
                "scope": annotation.scope,
                "region": annotation.region,
                "regions": annotation.regions,
                "event_type": annotation.event_type,
                "confidence": annotation.confidence,
                "metadata": {
                    "classifier_version": "election2026-v1",
                    "event_tags": list(getattr(annotation, "event_tags", ()) or ()),
                    "backfill": True,
                },
            }
            for url, annotation in annotations.items()
        ]
        election_rows += len(rows)
        for article_item in batch:
            source = sources.get(article_item.source_id, {})
            if source.get("topic") == "military" and source.get("military_source_type"):
                rows.append({
                    "url": article_item.url,
                    "topic": "military",
                    "source_type": source["military_source_type"],
                    "metadata": {"backfill": True},
                })
                military_rows += 1
        flush(rows)
        batch.clear()

    if batch:
        annotations = classify_election_articles(
            batch, election_config or {"enabled": False}, election_entities or {}
        )
        rows = [
            {
                "url": url,
                "topic": "election_2026_local",
                "scope": annotation.scope,
                "region": annotation.region,
                "regions": annotation.regions,
                "event_type": annotation.event_type,
                "confidence": annotation.confidence,
                "metadata": {
                    "classifier_version": "election2026-v1",
                    "event_tags": list(getattr(annotation, "event_tags", ()) or ()),
                    "backfill": True,
                },
            }
            for url, annotation in annotations.items()
        ]
        election_rows += len(rows)
        for article_item in batch:
            source = sources.get(article_item.source_id, {})
            if source.get("topic") == "military" and source.get("military_source_type"):
                rows.append({
                    "url": article_item.url,
                    "topic": "military",
                    "source_type": source["military_source_type"],
                    "metadata": {"backfill": True},
                })
                military_rows += 1
        flush(rows)

    return {
        "scanned": scanned,
        "election_topics": election_rows,
        "military_topics": military_rows,
        "written": written,
        "dry_run": int(dry_run),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill news topics only")
    parser.add_argument("--db", default=None, help="database path override")
    parser.add_argument("--sources-config", default=None)
    parser.add_argument("--election-config", default=None)
    parser.add_argument("--entities-config", default=None)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--since", default=None, help="fetched_at ISO lower bound")
    parser.add_argument("--until", default=None, help="fetched_at ISO upper bound")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    root = load_environment(Path(__file__).resolve().parent.parent)
    settings = get_settings(root, load_env=False)
    db_path = Path(args.db) if args.db else settings.news_db_path
    if not db_path.is_absolute():
        db_path = root / db_path
    source_path = Path(args.sources_config) if args.sources_config else settings.sources_config_path
    if not source_path.is_absolute():
        source_path = root / source_path
    election_path = Path(args.election_config) if args.election_config else root / "config/election_2026.yaml"
    entity_path = Path(args.entities_config) if args.entities_config else root / "config/election_2026_entities.yaml"
    if not election_path.is_absolute():
        election_path = root / election_path
    if not entity_path.is_absolute():
        entity_path = root / entity_path

    db = Database(db_path, read_only=args.dry_run)
    db.connect()
    try:
        result = backfill_topics(
            db,
            sources=_load_sources(source_path),
            election_config=load_election_config(election_path),
            election_entities=load_entities(entity_path),
            batch_size=args.batch_size,
            since=_parse_datetime(args.since),
            until=_parse_datetime(args.until),
            dry_run=args.dry_run,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    main()
