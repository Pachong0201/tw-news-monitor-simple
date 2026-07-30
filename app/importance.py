import yaml
import logging
from pathlib import Path
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# Default config path relative to project root
DEFAULT_RULES_PATH = "config/importance_rules.yaml"


class ImportanceResult:
    """Result of importance classification for one article."""

    __slots__ = ("score", "level", "matched_rules", "reasons")

    def __init__(
        self,
        score: int = 0,
        level: str = "normal",
        matched_rules: list[str] | None = None,
        reasons: list[str] | None = None,
    ):
        self.score = score
        self.level = level
        self.matched_rules = matched_rules or []
        self.reasons = reasons or []

    def __repr__(self) -> str:
        return (
            f"ImportanceResult(score={self.score}, level={self.level}, "
            f"rules={self.matched_rules})"
        )


def load_rules(config_path: str | Path) -> dict:
    """Load importance rules from YAML config."""
    with open(config_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    if not config.get("enabled", True):
        return {"enabled": False, "thresholds": {}, "rules": []}

    return config


def _match_keywords(text: str, keywords: list) -> list[str]:
    """Check which keywords appear in the text (case-insensitive)."""
    text_lower = text.lower()
    matched = []
    for kw in keywords:
        kw_str = str(kw)
        if kw_str.lower() in text_lower:
            matched.append(kw_str)
    return matched


def _contains_any(text: str, keywords: list[str]) -> bool:
    return len(_match_keywords(text, keywords)) > 0


def score_article(
    title: str,
    source_name: str,
    category: str,
    description: str,
    rules_config: dict,
) -> ImportanceResult:
    """Score a single article based on configured rules."""

    if not rules_config.get("enabled", True):
        return ImportanceResult()

    rules = rules_config.get("rules", [])
    thresholds = rules_config.get("thresholds", {})
    critical_th = thresholds.get("critical", 80)
    important_th = thresholds.get("important", 55)

    combined_text = f"{title} {description}".lower()
    source_lower = source_name.lower()
    category_lower = category.lower()

    matched_rules = []
    matched_reasons = []
    total_dimensions = {}

    for rule in rules:
        subjects = rule.get("subjects", [])
        actions = rule.get("actions", [])
        scenes = rule.get("scenes", [])
        negative = rule.get("negative", [])

        # Check negatives first - if any negative keyword matches, skip this rule
        if _contains_any(combined_text, negative):
            continue

        # Check subjects
        has_subject = _contains_any(combined_text, subjects)

        # Check actions
        has_action = _contains_any(combined_text, actions)

        # Check scenes
        has_scene = _contains_any(combined_text, scenes)

        # A rule matches if we have (subject + action) or (scene)
        if not (has_subject and has_action) and not has_scene:
            continue

        matched_rules.append(rule["id"])

        # Build dimension scores
        dims = rule.get("dimensions", {})
        for dim_name, dim_score in dims.items():
            total_dimensions[dim_name] = max(
                total_dimensions.get(dim_name, 0), dim_score
            )

        # Add reasons
        if has_subject:
            matched_subjects = _match_keywords(combined_text, subjects)
            matched_reasons.append(f"主体:{','.join(matched_subjects[:3])}")
        if has_action:
            matched_actions = _match_keywords(combined_text, actions)
            matched_reasons.append(
                f"动作:{','.join(matched_actions[:3])}"
            )
        if has_scene:
            matched_scenes = _match_keywords(combined_text, scenes)
            matched_reasons.append(f"场景:{','.join(matched_scenes[:3])}")

    if not matched_rules:
        return ImportanceResult()

    # Calculate base score
    base_score = sum(total_dimensions.values())

    # Apply rule weight
    weights = [r.get("weight", 1.0) for r in rules if r["id"] in matched_rules]
    if weights:
        base_score = int(base_score * max(weights))

    # Final score clamped to 0-100
    final_score = max(0, min(100, base_score))

    # Determine level
    if final_score >= critical_th:
        level = "critical"
    elif final_score >= important_th:
        level = "important"
    else:
        level = "normal"

    return ImportanceResult(
        score=final_score,
        level=level,
        matched_rules=matched_rules,
        reasons=matched_reasons,
    )


def classify_articles(
    articles: list,
    rules_config: dict,
    title_attr: str = "title",
    source_attr: str = "source_name",
    category_attr: str = "category",
    desc_attr: str = "description",
) -> list:
    """Classify a list of articles and return (article, result) pairs."""
    results = []
    for article in articles:
        title = getattr(article, title_attr, "")
        source = getattr(article, source_attr, "")
        category = getattr(article, category_attr, "")
        desc = getattr(article, desc_attr, "")

        result = score_article(title, source, category, desc, rules_config)
        results.append((article, result))
    return results


def importance_summary(results: list[ImportanceResult]) -> str:
    """Generate a one-line summary of classification results."""
    counts = {"critical": 0, "important": 0, "normal": 0}
    for _, r in results:
        counts[r.level] = counts.get(r.level, 0) + 1
    return (
        f"Importance summary: "
        f"critical={counts['critical']} "
        f"important={counts['important']} "
        f"normal={counts['normal']}"
    )


def validate_rules_config(config: dict) -> list[str]:
    """Validate importance rules configuration. Returns list of errors."""
    errors = []
    if not isinstance(config.get("enabled"), bool):
        errors.append("enabled must be a boolean")
    th = config.get("thresholds", {})
    for level in ("critical", "important", "normal"):
        val = th.get(level)
        if val is None or not isinstance(val, int):
            errors.append(f"thresholds.{level} must be an integer")
    if th.get("critical", 0) <= th.get("important", 0):
        errors.append("critical threshold must be > important threshold")
    if th.get("important", 0) <= th.get("normal", 0):
        errors.append("important threshold must be > normal threshold")
    rules = config.get("rules", [])
    rule_ids = set()
    for i, rule in enumerate(rules):
        rid = rule.get("id", f"<rule {i}>")
        if rid in rule_ids:
            errors.append(f"duplicate rule id: {rid}")
        rule_ids.add(rid)
        if not rule.get("dimensions"):
            errors.append(f"rule {rid}: missing dimensions")
    return errors



def select_highlights(
    importance_results: list,
    max_highlights: int = 10,
    default_published_at: datetime | None = None,
) -> list:
    """Select and sort highlight articles from importance classification results.

    Filters for critical and important articles, sorted by:
    - level: critical before important
    - score: descending
    - published_at: descending (newest first, None last)

    Returns a list of (article, ImportanceResult) tuples limited to max_highlights.
    """
    highlights = [
        (a, r) for a, r in importance_results
        if r.level in ("critical", "important")
    ]

    def _sort_key(item: tuple) -> tuple:
        article, result = item
        level_order = 0 if result.level == "critical" else 1
        score_neg = -result.score
        published = article.published_at if article.published_at else default_published_at
        pub_ts = published.timestamp() if published else 0
        pub_neg = -pub_ts
        return (level_order, score_neg, pub_neg)

    highlights.sort(key=_sort_key)

    return highlights[:max_highlights]
