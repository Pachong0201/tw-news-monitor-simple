from datetime import datetime, timezone
from pathlib import Path

from app.database import Database
from app.delivery import process_due_deliveries
from app.models import Article
from app.notifier import NotificationError


def make_db(tmp_path):
    db = Database(tmp_path / "news.db")
    db.connect()
    db.create_tables()
    return db


def test_failed_delivery_stays_pending_and_next_row_runs(tmp_path, monkeypatch):
    db = make_db(tmp_path)
    now = datetime(2026, 7, 31, tzinfo=timezone.utc)
    db.enqueue_delivery(
        "one", "text_digest", "feishu", {"text": "one"}, now=now,
    )
    db.enqueue_delivery(
        "two", "text_digest", "console", {"text": "two"}, now=now,
    )

    class Sender:
        def __init__(self, channel):
            self.channel = channel

        def send_long(self, text):
            if self.channel == "feishu":
                raise NotificationError("temporary")

    monkeypatch.setattr(
        "app.delivery.create_notifier", lambda channel: Sender(channel),
    )
    sent, failed = process_due_deliveries(db, Path(tmp_path), now=now)

    assert (sent, failed) == (1, 1)
    rows = db.conn.execute(
        "SELECT delivery_key, status, attempt_count FROM notification_outbox ORDER BY id"
    ).fetchall()
    assert rows == [("one", "pending", 1), ("two", "sent", 0)]
    db.close()


def test_malformed_payload_does_not_block_later_rows(tmp_path, monkeypatch):
    db = make_db(tmp_path)
    now = datetime(2026, 7, 31, tzinfo=timezone.utc)
    db.enqueue_delivery("bad", "text_digest", "console", {"text": "bad"}, now=now)
    db.conn.execute(
        "UPDATE notification_outbox SET payload_json='not-json' WHERE delivery_key='bad'"
    )
    db.conn.commit()
    db.enqueue_delivery("good", "text_digest", "console", {"text": "good"}, now=now)
    calls = []

    class Sender:
        def send_long(self, text):
            calls.append(text)

    monkeypatch.setattr("app.delivery.create_notifier", lambda channel: Sender())
    assert process_due_deliveries(db, Path(tmp_path), now=now) == (1, 1)
    assert calls == ["good"]
    db.close()


def test_successful_delivery_is_not_retried(tmp_path, monkeypatch):
    db = make_db(tmp_path)
    now = datetime(2026, 7, 31, tzinfo=timezone.utc)
    db.enqueue_delivery(
        "once", "text_digest", "console", {"text": "digest"}, now=now,
    )
    calls = []

    class Sender:
        def send_long(self, text):
            calls.append(text)

    monkeypatch.setattr("app.delivery.create_notifier", lambda channel: Sender())
    assert process_due_deliveries(db, Path(tmp_path), now=now) == (1, 0)
    assert process_due_deliveries(db, Path(tmp_path), now=now) == (0, 0)
    assert calls == ["digest"]
    db.close()


def test_missing_word_is_rebuilt_from_persisted_payload(tmp_path, monkeypatch):
    db = make_db(tmp_path)
    now = datetime(2026, 7, 31, 4, 5, tzinfo=timezone.utc)
    article = Article(
        source_id="test", source_name="测试", category="politics",
        title="新闻", url="https://example.com/1", published_at=now,
        fetched_at=now, position=1,
    )
    db.save_article(article)
    relative_path = "data/reports/outbox/run/台湾新闻监测_2026-07-31_0405.docx"
    db.enqueue_delivery(
        "word", "word_document", "feishu_app",
        {
            "article_urls": [article.url],
            "catch_up_urls": [],
            "generated_at": now.isoformat(),
            "output_path": relative_path,
            "importance_results": [{
                "url": article.url, "score": 0, "level": "normal",
                "matched_rules": [], "reasons": [],
            }],
        },
        now=now,
    )
    monkeypatch.setenv("FEISHU_APP_ID", "id")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    monkeypatch.setenv("FEISHU_CHAT_ID", "chat")
    sent = []
    monkeypatch.setattr(
        "app.delivery.send_document",
        lambda path, *args: sent.append(Path(path)),
    )

    assert process_due_deliveries(db, Path(tmp_path), now=now) == (1, 0)
    expected = Path(tmp_path) / relative_path
    assert expected.exists()
    assert sent == [expected]
    db.close()
