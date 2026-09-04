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
        self._migrate_article_columns()
        self._ensure_news_topics_table()

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
                position INTEGER NOT NULL,
                summary TEXT,
                summary_source TEXT,
                summary_attempted_at TEXT,
                section TEXT,
                language TEXT,
                access_level TEXT
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
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS news_topics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                topic TEXT NOT NULL DEFAULT 'election_2026_local',
                scope TEXT,
                region TEXT,
                regions_json TEXT,
                event_type TEXT,
                confidence INTEGER,
                source_type TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(url, topic)
            )
            """
        )
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_news_topics_url
            ON news_topics(url)
            """
        )
        self.conn.commit()

    def _migrate_article_columns(self) -> None:
        """Add columns to databases created before the feature.

        Append-only migration: older databases get the newer columns added
        (nullable, old rows keep NULL). No schema version table is used.
        """
        table = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='articles'"
        ).fetchone()
        if not table:
            return
        cols = [row[1] for row in self.conn.execute("PRAGMA table_info(articles)")]
        for col, ddl in (
            ("summary", "summary TEXT"),
            ("summary_source", "summary_source TEXT"),
            ("summary_attempted_at", "summary_attempted_at TEXT"),
            # 国际媒体免费监测层 Phase I（2026-08-13）
            ("section", "section TEXT"),
            ("language", "language TEXT"),
            ("access_level", "access_level TEXT"),
        ):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE articles ADD COLUMN {ddl}")
        self.conn.commit()

    def _ensure_news_topics_table(self) -> None:
        """Create or upgrade the multi-topic table without losing old rows."""
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS news_topics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                topic TEXT NOT NULL DEFAULT 'election_2026_local',
                scope TEXT,
                region TEXT,
                regions_json TEXT,
                event_type TEXT,
                confidence INTEGER,
                source_type TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(url, topic)
            )
            """
        )
        columns = {
            row[1] for row in self.conn.execute("PRAGMA table_info(news_topics)")
        }
        unique_indexes: list[tuple[str, ...]] = []
        for index in self.conn.execute("PRAGMA index_list(news_topics)"):
            if index[2]:
                unique_indexes.append(
                    tuple(
                        row[2]
                        for row in self.conn.execute(
                            f"PRAGMA index_info('{index[1]}')"
                        )
                    )
                )

        if ("url",) in unique_indexes:
            source_type_expr = "source_type" if "source_type" in columns else "NULL"
            try:
                self.conn.execute("BEGIN")
                self.conn.execute(
                    """
                    CREATE TABLE news_topics_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        url TEXT NOT NULL,
                        topic TEXT NOT NULL DEFAULT 'election_2026_local',
                        scope TEXT,
                        region TEXT,
                        regions_json TEXT,
                        event_type TEXT,
                        confidence INTEGER,
                        source_type TEXT,
                        created_at TEXT NOT NULL,
                        UNIQUE(url, topic)
                    )
                    """
                )
                self.conn.execute(
                    f"""
                    INSERT INTO news_topics_new
                        (id, url, topic, scope, region, regions_json,
                         event_type, confidence, source_type, created_at)
                    SELECT id, url, topic, scope, region, regions_json,
                           event_type, confidence, {source_type_expr}, created_at
                    FROM news_topics
                    """
                )
                self.conn.execute("DROP TABLE news_topics")
                self.conn.execute("ALTER TABLE news_topics_new RENAME TO news_topics")
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        elif "source_type" not in columns:
            self.conn.execute("ALTER TABLE news_topics ADD COLUMN source_type TEXT")

        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_news_topics_url
            ON news_topics(url)
            """
        )
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_news_topics_topic
            ON news_topics(topic)
            """
        )
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
                 published_at, fetched_at, position, summary, summary_source,
                 summary_attempted_at, section, language, access_level)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                article.summary,
                article.summary_source,
                article.summary_attempted_at.isoformat() if article.summary_attempted_at else None,
                article.section,
                article.language,
                article.access_level,
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
                     published_at, fetched_at, position, summary, summary_source,
                     summary_attempted_at, section, language, access_level)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    article.summary,
                    article.summary_source,
                    article.summary_attempted_at.isoformat() if article.summary_attempted_at else None,
                    article.section,
                    article.language,
                    article.access_level,
                ),
            )
            if cursor.rowcount > 0:
                inserted.append(article)
        self.conn.commit()
        return inserted

    def get_articles_since(self, time: datetime) -> list[Article]:
        rows = self.conn.execute(
            "SELECT source_id, source_name, category, title, url, "
            "published_at, fetched_at, position, summary, summary_source, "
            "summary_attempted_at, section, language, access_level "
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
                summary=row[8],
                summary_source=row[9],
                summary_attempted_at=datetime.fromisoformat(row[10]) if row[10] else None,
                section=row[11],
                language=row[12],
                access_level=row[13],
            )
            for row in rows
        ]

    def update_article_summaries(
        self, summaries: dict[str, str], source: str = "llm", attempted_at=None
    ) -> None:
        """Write generated summaries back to the database by URL."""
        for url, summary in summaries.items():
            self.conn.execute(
                "UPDATE articles SET summary = ?, summary_source = ?, "
                "summary_attempted_at = ? WHERE url = ?",
                (
                    summary,
                    source,
                    attempted_at.isoformat() if attempted_at else None,
                    url,
                ),
            )
        self.conn.commit()

    def mark_summary_attempted(self, urls: list[str], attempted_at=None) -> None:
        """Record a failed summary attempt (negative cache for retries)."""
        attempted_at = attempted_at or datetime.now()
        for url in urls:
            self.conn.execute(
                "UPDATE articles SET summary_attempted_at = ? "
                "WHERE url = ? AND summary IS NULL",
                (attempted_at.isoformat(), url),
            )
        self.conn.commit()

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

    def save_election_topic(
        self,
        url: str,
        *,
        scope: str,
        region: str | None,
        regions: list[str],
        event_type: str,
        confidence: int,
        created_at: datetime | None = None,
        topic: str = "election_2026_local",
    ) -> None:
        """Persist one article's election-topic annotation (idempotent)."""
        self.save_topic(
            url,
            topic,
            scope=scope,
            region=region,
            regions=regions,
            event_type=event_type,
            confidence=confidence,
            created_at=created_at,
        )

    def save_military_topic(
        self,
        url: str,
        *,
        source_type: str,
        created_at: datetime | None = None,
    ) -> None:
        """Persist a military topic while retaining any other URL topics."""
        if source_type not in {"commercial_military", "official_military"}:
            raise ValueError(f"Unsupported military source_type: {source_type}")
        self.save_topic(
            url,
            "military",
            source_type=source_type,
            created_at=created_at,
        )

    def save_topic(
        self,
        url: str,
        topic: str,
        *,
        scope: str | None = None,
        region: str | None = None,
        regions: list[str] | None = None,
        event_type: str | None = None,
        confidence: int | None = None,
        source_type: str | None = None,
        created_at: datetime | None = None,
    ) -> None:
        """Upsert one (URL, topic) annotation without replacing other topics."""
        import json

        created_at = created_at or datetime.now()
        self.conn.execute(
            """
            INSERT INTO news_topics
                (url, topic, scope, region, regions_json, event_type,
                 confidence, source_type, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url, topic) DO UPDATE SET
                scope = excluded.scope,
                region = excluded.region,
                regions_json = excluded.regions_json,
                event_type = excluded.event_type,
                confidence = excluded.confidence,
                source_type = excluded.source_type,
                created_at = excluded.created_at
            """,
            (
                url,
                topic,
                scope,
                region,
                json.dumps(regions or [], ensure_ascii=False),
                event_type,
                confidence,
                source_type,
                created_at.isoformat(),
            ),
        )
        self.conn.commit()

    def get_election_topic(self, url: str) -> dict | None:
        """Return one article's persisted election-topic row (or None)."""
        import json

        row = self.conn.execute(
            "SELECT url, topic, scope, region, regions_json, event_type, "
            "confidence, created_at FROM news_topics "
            "WHERE url = ? AND topic = 'election_2026_local'",
            (url,),
        ).fetchone()
        if not row:
            return None
        regions = []
        if row[4]:
            try:
                regions = json.loads(row[4])
            except ValueError:
                regions = []
        return {
            "url": row[0],
            "topic": row[1],
            "scope": row[2],
            "region": row[3],
            "regions": regions,
            "event_type": row[5],
            "confidence": row[6],
            "created_at": row[7],
        }

    def get_election_topics_by_urls(self, urls: list[str]) -> dict[str, dict]:
        """Return persisted topics for the given urls: {url: row_dict}."""
        if not urls:
            return {}
        placeholders = ",".join("?" for _ in urls)
        rows = self.conn.execute(
            f"SELECT url, topic, scope, region, regions_json, event_type, "
            f"confidence, created_at FROM news_topics "
            f"WHERE topic = 'election_2026_local' AND url IN ({placeholders})",
            urls,
        ).fetchall()
        result: dict[str, dict] = {}
        for row in rows:
            import json

            regions = []
            if row[4]:
                try:
                    regions = json.loads(row[4])
                except ValueError:
                    regions = []
            result[row[0]] = {
                "url": row[0],
                "topic": row[1],
                "scope": row[2],
                "region": row[3],
                "regions": regions,
                "event_type": row[5],
                "confidence": row[6],
                "created_at": row[7],
            }
        return result

    def get_topic_urls(self, urls: list[str], topic: str) -> set[str]:
        """Return the subset of URLs carrying the requested topic."""
        if not urls:
            return set()
        placeholders = ",".join("?" for _ in urls)
        rows = self.conn.execute(
            f"SELECT url FROM news_topics "
            f"WHERE topic = ? AND url IN ({placeholders})",
            [topic, *urls],
        ).fetchall()
        return {row[0] for row in rows}

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.close()


def build_db_path(db_name: str = "news.db") -> Path:
    return Path(__file__).resolve().parent.parent / "data" / db_name
