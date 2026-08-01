import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import Article


class Database:
    def __init__(self, db_path: str | Path):
        self._db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._conn

    def create_tables(self) -> None:
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id TEXT NOT NULL,
                source_name TEXT NOT NULL,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                published_at TEXT,
                fetched_at TEXT NOT NULL,
                position INTEGER NOT NULL
            )
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_articles_url
            ON articles(url)
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_articles_fetched_at
            ON articles(fetched_at)
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_articles_category
            ON articles(category)
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS notification_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                delivery_key TEXT NOT NULL UNIQUE,
                delivery_type TEXT NOT NULL,
                channel TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'sent')),
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT NOT NULL,
                last_error TEXT,
                created_at TEXT NOT NULL,
                sent_at TEXT
            )
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_outbox_due
            ON notification_outbox(status, next_attempt_at, id)
        """)
        self.conn.commit()

    @contextmanager
    def transaction(self):
        """Commit all enclosed writes together, or roll them all back."""
        self.conn.execute("BEGIN")
        try:
            yield
        except Exception:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

    def article_exists(self, url: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM articles WHERE url = ?", (url,)
        ).fetchone()
        return row is not None

    def save_article(self, article: Article, *, commit: bool = True) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO articles
                (source_id, source_name, category, title, url,
                 published_at, fetched_at, position)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                article.source_id,
                article.source_name,
                article.category,
                article.title,
                article.url,
                article.published_at.isoformat() if article.published_at else None,
                article.fetched_at.isoformat(),
                article.position,
            ),
        )
        if commit:
            self.conn.commit()

    def save_articles(
        self, articles: list[Article], *, commit: bool = True,
    ) -> list[Article]:
        inserted: list[Article] = []
        for article in articles:
            cursor = self.conn.execute(
                """
                INSERT OR IGNORE INTO articles
                    (source_id, source_name, category, title, url,
                     published_at, fetched_at, position)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    article.source_id,
                    article.source_name,
                    article.category,
                    article.title,
                    article.url,
                    article.published_at.isoformat() if article.published_at else None,
                    article.fetched_at.isoformat(),
                    article.position,
                ),
            )
            if cursor.rowcount > 0:
                inserted.append(article)
        if commit:
            self.conn.commit()
        return inserted

    def enqueue_delivery(
        self,
        delivery_key: str,
        delivery_type: str,
        channel: str,
        payload: dict,
        *,
        now: datetime | None = None,
        commit: bool = True,
    ) -> bool:
        """Add an idempotent pending delivery. Returns True when inserted."""
        created = now or datetime.now(timezone.utc)
        payload = {**payload, "schema_version": 1}
        cursor = self.conn.execute(
            """
            INSERT OR IGNORE INTO notification_outbox
                (delivery_key, delivery_type, channel, payload_json,
                 status, attempt_count, next_attempt_at, created_at)
            VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)
            """,
            (
                delivery_key,
                delivery_type,
                channel,
                json.dumps(payload, ensure_ascii=False),
                created.isoformat(),
                created.isoformat(),
            ),
        )
        if commit:
            self.conn.commit()
        return cursor.rowcount > 0

    def get_due_deliveries(
        self, now: datetime | None = None, limit: int = 100,
    ) -> list[dict]:
        cutoff = (now or datetime.now(timezone.utc)).isoformat()
        rows = self.conn.execute(
            """
            SELECT * FROM notification_outbox
            WHERE status='pending' AND next_attempt_at<=?
            ORDER BY created_at, id LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()
        columns = [d[0] for d in self.conn.execute(
            "SELECT * FROM notification_outbox LIMIT 0"
        ).description]
        result = []
        for row in rows:
            item = dict(zip(columns, row))
            result.append(item)
        return result

    def mark_delivery_sent(
        self, delivery_id: int, *, now: datetime | None = None,
    ) -> None:
        sent_at = (now or datetime.now(timezone.utc)).isoformat()
        self.conn.execute(
            """UPDATE notification_outbox
               SET status='sent', sent_at=?, last_error=NULL
               WHERE id=?""",
            (sent_at, delivery_id),
        )
        self.conn.commit()

    def mark_delivery_failed(
        self, delivery_id: int, error: str, *, now: datetime | None = None,
    ) -> None:
        current = now or datetime.now(timezone.utc)
        row = self.conn.execute(
            "SELECT attempt_count FROM notification_outbox WHERE id=?",
            (delivery_id,),
        ).fetchone()
        if row is None:
            return
        attempt_count = int(row[0]) + 1
        delay_minutes = min(360, 2 ** min(attempt_count - 1, 9))
        self.conn.execute(
            """UPDATE notification_outbox
               SET attempt_count=?, next_attempt_at=?, last_error=?
               WHERE id=?""",
            (
                attempt_count,
                (current + timedelta(minutes=delay_minutes)).isoformat(),
                str(error)[:2000],
                delivery_id,
            ),
        )
        self.conn.commit()

    def get_articles_by_urls(self, urls: list[str]) -> list[Article]:
        if not urls:
            return []
        placeholders = ",".join("?" for _ in urls)
        rows = self.conn.execute(
            "SELECT source_id, source_name, category, title, url, "
            "published_at, fetched_at, position FROM articles "
            f"WHERE url IN ({placeholders})",
            urls,
        ).fetchall()
        by_url = {
            row[4]: Article(
                source_id=row[0], source_name=row[1], category=row[2],
                title=row[3], url=row[4],
                published_at=datetime.fromisoformat(row[5]) if row[5] else None,
                fetched_at=datetime.fromisoformat(row[6]), position=row[7],
            )
            for row in rows
        }
        return [by_url[url] for url in urls if url in by_url]

    def get_articles_since(self, time: datetime) -> list[Article]:
        rows = self.conn.execute(
            "SELECT source_id, source_name, category, title, url, "
            "published_at, fetched_at, position "
            "FROM articles WHERE fetched_at >= ? "
            "ORDER BY category, position, published_at",
            (time.isoformat(),),
        ).fetchall()
        return [
            Article(
                source_id=row[0],
                source_name=row[1],
                category=row[2],
                title=row[3],
                url=row[4],
                published_at=datetime.fromisoformat(row[5]) if row[5] else None,
                fetched_at=datetime.fromisoformat(row[6]),
                position=row[7],
            )
            for row in rows
        ]

    def count_articles(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) FROM articles").fetchone()
        return row[0] if row else 0

    def count_by_category(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT category, COUNT(*) FROM articles GROUP BY category"
        ).fetchall()
        return dict(rows) if rows else {}

    def count_by_source(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT source_id, COUNT(*) FROM articles GROUP BY source_id"
        ).fetchall()
        return dict(rows) if rows else {}

    def get_total_count(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM articles"
        ).fetchone()
        return row[0] if row else 0

    def get_all_article_urls(self) -> list[str]:
        """Return all article URLs from the database.
        Used for building historical identity keys for UDN alias dedup.
        """
        rows = self.conn.execute("SELECT url FROM articles").fetchall()
        return [row[0] for row in rows]

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.close()


def build_db_path(db_name: str = "news.db") -> Path:
    return Path(__file__).resolve().parent.parent / "data" / db_name
