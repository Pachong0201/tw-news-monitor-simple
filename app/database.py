import sqlite3
from datetime import datetime
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
        self.conn.commit()

    def article_exists(self, url: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM articles WHERE url = ?", (url,)
        ).fetchone()
        return row is not None

    def save_article(self, article: Article) -> None:
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
        self.conn.commit()

    def save_articles(self, articles: list[Article]) -> list[Article]:
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
        self.conn.commit()
        return inserted

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
