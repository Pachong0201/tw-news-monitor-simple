import tempfile
from datetime import datetime
from pathlib import Path

import pytest
from docx import Document

from app.models import Article
from app.word_digest import build_word_digest


def make_article(**kwargs):
    now = datetime(2026, 7, 14, 20, 30)
    return Article(
        source_id=kwargs.get("source_id", "test"),
        source_name=kwargs.get("source_name", "中央社"),
        category=kwargs.get("category", "politics"),
        title=kwargs.get("title", "測試新聞"),
        url=kwargs.get("url", "https://example.com/news/1"),
        published_at=kwargs.get("published_at", now),
        fetched_at=now,
        position=kwargs.get("position", 1),
    )


class TestWordDigest:
    def test_generates_docx(self):
        """1. Successfully generate a .docx file."""
        articles = [make_article()]
        with tempfile.TemporaryDirectory() as tmp:
            output = build_word_digest(articles, Path(tmp))
            assert output.suffix == ".docx"
            assert output.exists()

    def test_auto_creates_output_dir(self):
        """2. Output directory is created if not exists."""
        articles = [make_article()]
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "reports" / "sub"
            output = build_word_digest(articles, output_dir)
            assert output.parent.exists()

    def test_contains_title(self):
        """3. Word contains the main title."""
        articles = [make_article()]
        with tempfile.TemporaryDirectory() as tmp:
            output = build_word_digest(articles, Path(tmp))
            doc = Document(str(output))
            texts = [p.text for p in doc.paragraphs]
            assert any("台湾新闻监测" in t for t in texts)

    def test_contains_categories(self):
        """4. Word contains politics, economy, international."""
        articles = [
            make_article(category="politics", url="https://example.com/1"),
            make_article(category="economy", url="https://example.com/2"),
            make_article(category="international", url="https://example.com/3"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            output = build_word_digest(articles, Path(tmp))
            doc = Document(str(output))
            texts = [p.text for p in doc.paragraphs]
            assert any("政治" in t for t in texts)
            assert any("经济" in t for t in texts)
            assert any("国际" in t for t in texts)

    def test_contains_article_title(self):
        """5. Word contains article titles."""
        articles = [make_article(title="獨家新聞標題")]
        with tempfile.TemporaryDirectory() as tmp:
            output = build_word_digest(articles, Path(tmp))
            doc = Document(str(output))
            texts = [p.text for p in doc.paragraphs]
            assert any("獨家新聞標題" in t for t in texts)

    def test_contains_media_name(self):
        """6. Word contains media name."""
        articles = [make_article(source_name="中央社")]
        with tempfile.TemporaryDirectory() as tmp:
            output = build_word_digest(articles, Path(tmp))
            doc = Document(str(output))
            texts = [p.text for p in doc.paragraphs]
            assert any("中央社" in t for t in texts)

    def test_contains_publish_time(self):
        """7. Word contains publish time."""
        articles = [make_article(published_at=datetime(2026, 7, 14, 20, 30))]
        with tempfile.TemporaryDirectory() as tmp:
            output = build_word_digest(articles, Path(tmp))
            doc = Document(str(output))
            texts = [p.text for p in doc.paragraphs]
            assert any("2026-07-14 20:30" in t for t in texts)

    def test_contains_url(self):
        """8. Word contains original link text."""
        articles = [make_article(url="https://example.com/test-article")]
        with tempfile.TemporaryDirectory() as tmp:
            output = build_word_digest(articles, Path(tmp))
            doc = Document(str(output))
            texts = [p.text for p in doc.paragraphs]
            assert any("https://example.com/test-article" in t for t in texts)

    def test_chinese_text(self):
        """9. Chinese text is written correctly."""
        articles = [make_article(title="繁體中文測試新聞")]
        with tempfile.TemporaryDirectory() as tmp:
            output = build_word_digest(articles, Path(tmp))
            doc = Document(str(output))
            texts = [p.text for p in doc.paragraphs]
            assert any("繁體中文測試新聞" in t for t in texts)

    def test_filename_format(self):
        """10. Filename format is correct."""
        articles = [make_article()]
        dt = datetime(2026, 7, 14, 20, 30)
        with tempfile.TemporaryDirectory() as tmp:
            output = build_word_digest(articles, Path(tmp), generated_at=dt)
            assert "台湾新闻监测_2026-07-14_2030" in output.name
            assert output.name.endswith(".docx")

    def test_no_articles_raises(self):
        """13. No articles raises ValueError, no file generated."""
        with tempfile.TemporaryDirectory() as tmp:
            with pytest.raises(ValueError, match="No articles"):
                build_word_digest([], Path(tmp))

    def test_contains_footer(self):
        """Word contains the footer disclaimer."""
        articles = [make_article()]
        with tempfile.TemporaryDirectory() as tmp:
            output = build_word_digest(articles, Path(tmp))
            doc = Document(str(output))
            texts = [p.text for p in doc.paragraphs]
            assert any("本简报由自动新闻监测程序生成" in t for t in texts)

    def test_count_meta(self):
        """Meta line shows correct article count."""
        articles = [make_article() for _ in range(5)]
        with tempfile.TemporaryDirectory() as tmp:
            output = build_word_digest(articles, Path(tmp))
            doc = Document(str(output))
            texts = [p.text for p in doc.paragraphs]
            assert any("5条" in t for t in texts)

    def test_hyperlink_in_xml(self):
        """URL is added as a clickable hyperlink."""
        articles = [make_article(url="https://example.com/hyperlink-test")]
        with tempfile.TemporaryDirectory() as tmp:
            output = build_word_digest(articles, Path(tmp))
            doc = Document(str(output))
            xml = doc.element.xml
            assert "w:hyperlink" in xml
