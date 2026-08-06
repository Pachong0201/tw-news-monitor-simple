"""Content filtering for news outside the user's focus area.

The collector layer intentionally does NOT filter by keywords, so this
module is applied as an explicit delivery/ingest gate configured in
``config/content_filter.yaml``. It exists because the user asked to
exclude social trivia that frequently appears in economy feeds.
"""

import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

DEFAULT_FILTER_PATH = "config/content_filter.yaml"
DEFAULT_CATEGORY_KEY = "_default"


def load_content_filter(config_path: str | Path | None = None) -> dict:
    """Load the content filter config.

    Missing or invalid files yield a disabled config so collection
    never breaks because of a filter file.
    """
    if config_path is None:
        config_path = Path(__file__).resolve().parent.parent / DEFAULT_FILTER_PATH
    path = Path(config_path)
    if not path.exists():
        return {"enabled": False, "mode": "drop_before_save", "categories": {}}
    try:
        with open(path, encoding="utf-8") as f:
            config = yaml.safe_load(f)
    except Exception as exc:
        logger.warning("Failed to load content filter %s: %s", path, exc)
        return {"enabled": False, "mode": "drop_before_save", "categories": {}}
    if not isinstance(config, dict):
        config = {}
    config.setdefault("enabled", False)
    config.setdefault("mode", "drop_before_save")
    config.setdefault("categories", {})
    return config


def _match_keywords(text: str, keywords: list) -> list[str]:
    """Return the subset of keywords found in text (case-insensitive)."""
    text_lower = text.lower()
    matched = []
    for kw in keywords or []:
        kw_str = str(kw).strip()
        if kw_str and kw_str.lower() in text_lower:
            matched.append(kw_str)
    return matched


def blocked_keywords(title: str, summary: str, category: str, config: dict) -> list[str]:
    """Return keywords from config that block this article."""
    categories = config.get("categories", {}) or {}
    keywords = list(categories.get(DEFAULT_CATEGORY_KEY, []) or [])
    keywords += list(categories.get(category, []) or [])
    text = f"{title} {summary}"
    return _match_keywords(text, keywords)


def filter_articles(articles: list, config: dict | None = None) -> tuple[list, list]:
    """Split articles into (kept, blocked) according to the filter config.

    Matching is a case-insensitive substring check against the title and
    RSS summary. A disabled or empty config keeps everything.
    """
    if not config or not config.get("enabled", False):
        return list(articles), []

    kept: list = []
    blocked: list = []
    for article in articles:
        title = getattr(article, "title", "") or ""
        summary = getattr(article, "summary", "") or ""
        category = getattr(article, "category", "") or ""
        matched = blocked_keywords(title, summary, category, config)
        if matched:
            blocked.append(article)
        else:
            kept.append(article)
    return kept, blocked
