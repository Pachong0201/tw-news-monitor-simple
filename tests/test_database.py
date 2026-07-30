import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from app.database import Database
from app.models import Article


@pytest.fixture
def db():
    """Create a temporary database for testing."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_path = tmp.name
    db = Database(db_path)
    db.connect()
    db.create_tables()
    yield db
    db.close()
    Path(db_path).unlink(missing_ok=True)


def make_article(url: str = "https://example.com/news/1", **kwargs) -> Article:
    now = datetime.now()
    return Article(
        source_id=kwargs.get("source_id", "test"),
        source_name=kwargs.get("source_name", "測試媒體"),
        category=kwargs.get("category", "politics"),
        title=kwargs.get("title", "測試新聞"),
        url=url,
        published_at=kwargs.get("published_at", now),
        fetched_at=kwargs.get("fetched_at", now),
        position=kwargs.get("position", 1),
    )


class TestDatabase:
    def test_create_tables(self, db):
        """1. Table creation succeeds."""
        # Tables should already exist from fixture
        rows = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='articles'"
        ).fetchall()
        assert len(rows) == 1

    def test_save_article(self, db):
        """2. Save article succeeds."""
        article = make_article()
        db.save_article(article)
        assert db.count_articles() == 1

    def test_duplicate_url_not_saved(self, db):
        """3. Duplicate URL is not saved again."""
        a1 = make_article(url="https://example.com/news/1")
        db.save_article(a1)
        assert db.count_articles() == 1

        a2 = make_article(url="https://example.com/news/1", title="重複標題")
        db.save_article(a2)
        assert db.count_articles() == 1

    def test_empty_query_returns_empty_list(self, db):
        """4. Empty query returns empty list."""
        articles = db.get_articles_since(datetime(2025, 1, 1))
        assert articles == []
        assert isinstance(articles, list)

    def test_db_file_deletable_after_close(self):
        """5. DB file can be deleted after close."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db_path = tmp.name
        db = Database(db_path)
        db.connect()
        db.create_tables()
        article = make_article()
        db.save_article(article)
        db.close()

        # Should be deletable after close
        Path(db_path).unlink()
        assert not Path(db_path).exists()

    def test_save_articles_bulk(self, db):
        """Save multiple articles and count."""
        articles = [
            make_article(url=f"https://example.com/news/{i}", position=i)
            for i in range(5)
        ]
        db.save_articles(articles)
        assert db.count_articles() == 5

    def test_article_exists(self, db):
        """Check article existence by URL."""
        url = "https://example.com/exists"
        assert not db.article_exists(url)
        db.save_article(make_article(url=url))
        assert db.article_exists(url)

    def test_get_articles_since(self, db):
        """Filter articles by fetched_at."""
        early = datetime(2025, 6, 1)
        late = datetime(2025, 7, 1)

        a1 = make_article(url="https://example.com/early", fetched_at=early)
        a2 = make_article(url="https://example.com/late", fetched_at=late)

        db.save_articles([a1, a2])

        # Should get only late
        result = db.get_articles_since(datetime(2025, 6, 15))
        assert len(result) == 1
        assert result[0].url == "https://example.com/late"

    def test_count_by_category(self, db):
        """Count articles grouped by category."""
        a1 = make_article(url="https://example.com/pol", category="politics")
        a2 = make_article(url="https://example.com/eco", category="economy")
        a3 = make_article(url="https://example.com/eco2", category="economy")
        db.save_articles([a1, a2, a3])

        counts = db.count_by_category()
        assert counts.get("politics") == 1
        assert counts.get("economy") == 2

    def test_empty_count_returns_empty_dict(self, db):
        """count_by_category on empty DB returns empty dict."""
        counts = db.count_by_category()
        assert counts == {}
        assert isinstance(counts, dict)

    def test_empty_count_by_source(self, db):
        """count_by_source on empty DB returns empty dict."""
        counts = db.count_by_source()
        assert counts == {}
        assert isinstance(counts, dict)

    def test_article_with_null_published_at(self, db):
        """Article with None published_at saves and retrieves OK."""
        now = datetime.now()
        article = Article(
            source_id="test",
            source_name="測試媒體",
            category="politics",
            title="無時間新聞",
            url="https://example.com/notime",
            published_at=None,
            fetched_at=now,
            position=1,
        )
        db.save_article(article)

        result = db.get_articles_since(datetime(2025, 1, 1))
        assert len(result) == 1
        assert result[0].published_at is None
        assert result[0].title == "無時間新聞"
