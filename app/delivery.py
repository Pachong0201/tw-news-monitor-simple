"""Durable notification delivery backed by the news database outbox."""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from .database import Database
from .feishu import send_card, send_document
from .importance import ImportanceResult
from .notifier import NotificationError, create_notifier
from .word_digest import build_word_digest

logger = logging.getLogger(__name__)


def serialize_importance_results(results: list) -> list[dict]:
    return [
        {
            "url": article.url,
            "score": result.score,
            "level": result.level,
            "matched_rules": result.matched_rules,
            "reasons": result.reasons,
        }
        for article, result in results
    ]


def _feishu_credentials() -> tuple[str, str, str]:
    load_dotenv()
    app_id = os.getenv("FEISHU_APP_ID", "").strip()
    app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
    chat_id = os.getenv("FEISHU_CHAT_ID", "").strip()
    if not app_id or not app_secret or not chat_id:
        raise NotificationError("Feishu app credentials are not configured")
    return app_id, app_secret, chat_id


def _resolve_output_path(project_root: Path, relative_path: str) -> Path:
    root = project_root.resolve()
    path = (root / relative_path).resolve()
    if path != root and root not in path.parents:
        raise ValueError("Outbox output path escapes the project directory")
    return path


def _process_one(db: Database, row: dict, project_root: Path) -> None:
    payload = json.loads(row["payload_json"])
    if payload.get("schema_version") != 1:
        raise ValueError("Unsupported outbox payload schema")

    delivery_type = row["delivery_type"]
    if delivery_type == "text_digest":
        create_notifier(row["channel"]).send_long(payload["text"])
        return

    if delivery_type == "word_document":
        app_id, app_secret, chat_id = _feishu_credentials()
        word_path = _resolve_output_path(project_root, payload["output_path"])
        if not word_path.exists():
            articles = db.get_articles_by_urls(payload["article_urls"])
            if len(articles) != len(payload["article_urls"]):
                raise ValueError("Cannot rebuild Word digest: articles are missing")
            by_url = {article.url: article for article in articles}
            importance_results = []
            for item in payload.get("importance_results", []):
                article = by_url.get(item["url"])
                if article is None:
                    continue
                importance_results.append((article, ImportanceResult(
                    score=item.get("score", 0),
                    level=item.get("level", "normal"),
                    matched_rules=item.get("matched_rules", []),
                    reasons=item.get("reasons", []),
                )))
            generated_at = datetime.fromisoformat(payload["generated_at"])
            built_path = build_word_digest(
                articles,
                word_path.parent,
                generated_at=generated_at,
                catch_up_urls=set(payload.get("catch_up_urls", [])),
                importance_results=importance_results,
            )
            if built_path.resolve() != word_path:
                raise ValueError("Word digest rebuilt at an unexpected path")
        send_document(word_path, app_id, app_secret, chat_id)
        return

    if delivery_type == "highlight_card":
        app_id, app_secret, chat_id = _feishu_credentials()
        send_card(payload["card"], app_id, app_secret, chat_id)
        return

    raise ValueError(f"Unknown delivery type: {delivery_type}")


def process_due_deliveries(
    db: Database,
    project_root: Path,
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> tuple[int, int]:
    """Attempt every due row independently; return (sent, failed)."""
    current = now or datetime.now(timezone.utc)
    sent = 0
    failed = 0
    for row in db.get_due_deliveries(current, limit=limit):
        try:
            _process_one(db, row, project_root)
        except Exception as exc:
            failed += 1
            db.mark_delivery_failed(row["id"], str(exc), now=current)
            logger.warning(
                "Outbox delivery failed: key=%s type=%s error=%s",
                row["delivery_key"], row["delivery_type"], exc,
            )
        else:
            sent += 1
            db.mark_delivery_sent(row["id"], now=current)
    return sent, failed
