from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.article_identity import article_identity_key
from app.collectors.base import BaseCollector
from app.collectors.military import (
    MNAMilitaryCollector,
    NownewsMilitaryCollector,
    _parse_ltn_date,
)
from app.database import Database, SCHEMA_VERSION
from app.election2026.config import load_election_config, load_entities
from app.freshness import filter_fresh_articles
from app.importance import classify_articles
from app.main import collect_all
from app.military import (
    _military_source_ids_cached,
    clear_military_source_cache,
    military_source_ids,
)
from app.models import Article
from app.settings import get_settings
from app.topic_backfill import backfill_topics


TAIPEI = ZoneInfo("Asia/Taipei")


def article(
    url: str,
    title: str = "测试新闻",
    *,
    source_id: str = "test",
    category: str = "politics",
    published_at: datetime | None = None,
    precision: str = "exact",
) -> Article:
    now = datetime(2026, 9, 8, 12, 0, tzinfo=TAIPEI)
    return Article(
        source_id=source_id,
        source_name="测试来源",
        category=category,
        title=title,
        url=url,
        published_at=published_at,
        fetched_at=now,
        position=1,
        published_at_precision=precision,
    )


def test_importance_prefers_level_then_score_and_uses_summary():
    rules = {
        "enabled": True,
        "thresholds": {"critical": 85, "important": 65, "normal": 0},
        "scoring": {"multi_rule_bonus": 0},
        "rules": [
            {
                "id": "critical_security",
                "track": "politics_security",
                "base_score": 86,
                "level_cap": "critical",
                "subjects": ["賴清德"],
                "actions": ["國防改革"],
                "scenes": [],
                "negative": [],
            },
            {
                "id": "important_policy",
                "track": "politics_security",
                "base_score": 95,
                "level_cap": "important",
                "subjects": ["國家安全"],
                "actions": ["政策"],
                "scenes": [],
                "negative": [],
            },
        ],
    }
    title = "賴清德今日出席活動"
    summary = "宣布新的國家安全與兩岸政策，並提出國防改革方案。"
    title_only = article("https://example.test/importance/title", title, published_at=None)
    title_only.summary = None
    title_result = classify_articles([title_only], rules)[0][1]
    assert title_result.level == "normal"

    full = article("https://example.test/importance/full", title)
    full.summary = summary
    result = classify_articles([full], rules)[0][1]
    assert result.level == "critical"
    assert result.score == 86

    reversed_rules = dict(rules)
    reversed_rules["rules"] = list(reversed(rules["rules"]))
    reversed_result = classify_articles([full], reversed_rules)[0][1]
    assert (reversed_result.level, reversed_result.score) == ("critical", 86)


def test_display_url_preserves_path_case_and_removes_tracking():
    raw = "https://example.com/News/ABC123?utm_source=test&fbclid=x#section"
    display = BaseCollector.normalize_url(raw)
    assert display == "https://example.com/News/ABC123"
    assert article_identity_key(raw) == article_identity_key(display)


def test_military_time_precision_and_rollover():
    now = datetime(2026, 9, 8, 0, 10, tzinfo=TAIPEI)
    parsed = _parse_ltn_date("更新時間 23:58", now)
    assert parsed == datetime(2026, 9, 7, 23, 58, tzinfo=TAIPEI)


def test_nownews_aware_and_naive_timestamps():
    collector = NownewsMilitaryCollector({
        "id": "nownews_military",
        "name": "NOWnews",
        "category": "military",
        "url": "https://www.nownews.com/cat/news-summary/military/",
    })
    html = """
    <ul id="ulNewsList">
      <li class="item"><a href="/news/1"><h3 class="title">aware</h3>
        <time datetime="2026-09-08T00:00:00+00:00">x</time></a></li>
      <li class="item"><a href="/news/2"><h3 class="title">naive</h3>
        <time datetime="2026-09-08T00:00:00">x</time></a></li>
    </ul>
    """
    rows, valid = collector._parse_page(html, datetime(2026, 9, 8, 12, tzinfo=TAIPEI))
    assert valid
    by_title = {row.title: row for row in rows}
    assert by_title["aware"].published_at == datetime(2026, 9, 8, 8, tzinfo=TAIPEI)
    assert by_title["naive"].published_at == datetime(2026, 9, 8, 0, tzinfo=TAIPEI)


def test_mna_date_only_is_not_exact_time():
    class Client:
        def get(self, url):
            import httpx
            request = httpx.Request("GET", url)
            return httpx.Response(
                200,
                request=request,
                text=(
                    '<a href="/news/detail/?UserKey=01234567-89ab-cdef-0123-456789abcdef">'
                    '<div class="title">date only</div>'
                    '<div class="summary">summary</div>'
                    '<span class="time">民國115年09月08日</span></a>'
                ),
            )

    collector = MNAMilitaryCollector({
        "id": "mna_military",
        "name": "MNA",
        "category": "military",
        "url": "https://mna.mnd.gov.tw/news/overview/",
    })
    collector._client = Client()
    rows = collector.collect()
    assert len(rows) == 1
    assert rows[0].published_at_precision == "date_only"
    result = filter_fresh_articles(
        rows,
        datetime(2026, 9, 8, 12, tzinfo=TAIPEI),
    )
    assert result.unknown_time_articles == rows


def test_schema_identity_and_batch_topics(tmp_path):
    db = Database(tmp_path / "news.db")
    db.connect()
    try:
        assert db.schema_version() == SCHEMA_VERSION
        columns = {row[1] for row in db.conn.execute("PRAGMA table_info(articles)")}
        assert {"identity_key", "published_at_precision"} <= columns
        assert db.save_articles([article("https://example.test/News/1")])
        row = db.conn.execute(
            "SELECT identity_key, published_at_precision FROM articles"
        ).fetchone()
        assert row[0] == article_identity_key("https://example.test/News/1")
        assert row[1] == "exact"
        db.save_topics([
            {"url": "https://example.test/News/1", "topic": "military", "source_type": "commercial_military"},
            {"url": "https://example.test/News/1", "topic": "election_2026_local", "scope": "national"},
        ])
        assert db.count_topics() == 2
        db.save_topics([{"url": "https://example.test/News/1", "topic": "military", "source_type": "official_military"}])
        assert db.count_topics() == 2
    finally:
        db.close()


def test_topic_ingest_happens_before_freshness(tmp_path):
    class Collector:
        def __init__(self, cfg):
            self.cfg = cfg

        def collect(self):
            old = datetime(2026, 8, 1, 12, tzinfo=TAIPEI)
            return [
                article("https://example.test/old", "2026九合一提名", published_at=old),
                article("https://example.test/unknown", "2026九合一提名", published_at=None),
            ]

        def close(self):
            pass

    db = Database(tmp_path / "topics.db")
    db.connect()
    try:
        result = collect_all(
            [{"id": "s", "name": "测试", "type": "stub", "category": "politics", "url": "https://example.test"}],
            db,
            collector_map={"stub": Collector},
            election_config=load_election_config(),
            election_entities=load_entities(),
        )
        assert len(result.inserted_articles) == 2
        assert db.get_topic_urls(
            ["https://example.test/old", "https://example.test/unknown"],
            "election_2026_local",
        ) == {"https://example.test/old", "https://example.test/unknown"}
    finally:
        db.close()


def test_topic_backfill_is_idempotent_and_dry_run_is_read_only(tmp_path):
    path = tmp_path / "backfill.db"
    db = Database(path)
    db.connect()
    db.save_articles([
        article("https://example.test/e", "2026九合一提名"),
        article("https://example.test/m", "军武新闻", source_id="mna_military", category="military"),
    ])
    db.close()
    cfg = load_election_config()
    entities = load_entities()
    sources = {"mna_military": {"topic": "military", "military_source_type": "official_military"}}

    ro = Database(path, read_only=True)
    ro.connect()
    try:
        preview = backfill_topics(
            ro, sources=sources, election_config=cfg, election_entities=entities,
            dry_run=True, batch_size=1,
        )
        assert preview["written"] == 0
        assert ro.count_topics() == 0
    finally:
        ro.close()

    db = Database(path)
    db.connect()
    try:
        first = backfill_topics(
            db, sources=sources, election_config=cfg, election_entities=entities,
            batch_size=1,
        )
        count = db.count_topics()
        second = backfill_topics(
            db, sources=sources, election_config=cfg, election_entities=entities,
            batch_size=1,
        )
        assert first["written"] >= 2
        assert second["written"] >= 2
        assert db.count_topics() == count
    finally:
        db.close()


def test_settings_path_priority_and_log_level(monkeypatch, tmp_path):
    for name in (
        "NEWS_DB_PATH", "DATABASE_PATH", "SOURCES_CONFIG_PATH", "LOG_LEVEL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABASE_PATH", "legacy.db")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    first = get_settings(tmp_path, load_env=False)
    assert first.news_db_path == tmp_path / "legacy.db"
    assert first.log_level == "DEBUG"
    monkeypatch.setenv("NEWS_DB_PATH", "new.db")
    second = get_settings(tmp_path, load_env=False)
    assert second.news_db_path == tmp_path / "new.db"


def test_military_source_cache_uses_custom_path_once(tmp_path):
    path = tmp_path / "sources.yaml"
    path.write_text(
        "sources:\n  - id: custom_military\n    topic: military\n    enabled: true\n",
        encoding="utf-8",
    )
    clear_military_source_cache()
    assert military_source_ids(path) == frozenset({"custom_military"})
    assert military_source_ids(path) == frozenset({"custom_military"})
    info = _military_source_ids_cached.cache_info()
    assert info.hits >= 1
