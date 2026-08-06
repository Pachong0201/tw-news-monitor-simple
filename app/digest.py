from datetime import datetime

from .models import Article


CATEGORY_NAMES = {
    "politics": "政治",
    "economy": "经济",
    "international": "国际",
}

CATEGORY_ORDER = ["politics", "economy", "international"]


def build_digest(articles: list[Article], fetched_at: datetime) -> str:
    """Build a digest text from newly fetched articles.

    Articles are grouped by category, sorted by (position, published_at, source_name).
    """
    if not articles:
        total = 0
        # Count source failures from context will be added by caller
        lines = [
            f"【台湾新闻监测｜{fetched_at.strftime('%Y-%m-%d %H:%M')}】",
            "",
            "本轮未发现新增新闻。",
        ]
        return "\n".join(lines)

    # Group by category
    grouped: dict[str, list[Article]] = {}
    for cat in CATEGORY_ORDER:
        grouped[cat] = []
    for article in articles:
        cat = article.category
        if cat in grouped:
            grouped[cat].append(article)

    lines: list[str] = []
    lines.append(f"【台湾新闻监测｜{fetched_at.strftime('%Y-%m-%d %H:%M')}】")
    lines.append("")

    for cat in CATEGORY_ORDER:
        cat_articles = grouped.get(cat, [])
        if not cat_articles:
            continue
        cat_name = CATEGORY_NAMES.get(cat, cat)
        # Sort by position, then published_at (desc), then source_name
        cat_articles.sort(key=lambda a: (
            a.position,
            -(a.published_at.timestamp() if a.published_at else 0),
            a.source_name,
        ))
        lines.append(f"{cat_name}｜新增{len(cat_articles)}条")
        for idx, article in enumerate(cat_articles, 1):
            time_str = ""
            if article.published_at:
                time_str = article.published_at.strftime("%H:%M")
            lines.append(f"{idx}. {article.title}")
            lines.append(f"   {article.source_name}｜{time_str}")
            lines.append(f"   {article.url}")
        lines.append("")

    return "\n".join(lines)


def count_new_by_category(articles: list[Article]) -> dict[str, int]:
    result: dict[str, int] = {}
    for a in articles:
        result[a.category] = result.get(a.category, 0) + 1
    return result
