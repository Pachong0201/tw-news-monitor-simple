import yaml
import logging
from pathlib import Path
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# Default config path relative to project root
DEFAULT_RULES_PATH = "config/importance_rules.yaml"

ALLOWED_TRACKS = ("election", "politics_security")
ALLOWED_LEVEL_CAPS = ("normal", "important", "critical")
LEVEL_ORDER = {"critical": 0, "important": 1, "normal": 2}


class ImportanceResult:
    """Result of importance classification for one article."""

    __slots__ = (
        "score",
        "level",
        "matched_rules",
        "reasons",
        "track",
        "matched_tracks",
        "capped",
    )

    def __init__(
        self,
        score: int = 0,
        level: str = "normal",
        matched_rules: list[str] | None = None,
        reasons: list[str] | None = None,
        track: str | None = None,
        matched_tracks: list[str] | None = None,
        capped: bool = False,
    ):
        self.score = score
        self.level = level
        self.matched_rules = matched_rules or []
        self.reasons = reasons or []
        self.track = track
        self.matched_tracks = matched_tracks or []
        self.capped = capped

    def __repr__(self) -> str:
        return (
            f"ImportanceResult(score={self.score}, level={self.level}, "
            f"track={self.track}, rules={self.matched_rules})"
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


def _rule_match(rule: dict, combined_text: str) -> dict | None:
    """Check one rule against combined title+description text.

    Returns matched subjects/actions/scenes/boosts, or None if the rule
    does not match (including negative keyword hits).
    """
    negative = rule.get("negative", [])
    if _contains_any(combined_text, negative):
        return None

    subjects = rule.get("subjects", [])
    actions = rule.get("actions", [])
    scenes = rule.get("scenes", [])

    matched_subjects = _match_keywords(combined_text, subjects)
    matched_actions = _match_keywords(combined_text, actions)
    matched_scenes = _match_keywords(combined_text, scenes)

    has_subject = len(matched_subjects) > 0
    has_action = len(matched_actions) > 0
    has_scene = len(matched_scenes) > 0

    if not ((has_subject and has_action) or has_scene):
        return None

    matched_boosts = []
    for boost in rule.get("boosts", []) or []:
        if _contains_any(combined_text, boost.get("keywords", [])):
            matched_boosts.append(boost)

    return {
        "matched_subjects": matched_subjects,
        "matched_actions": matched_actions,
        "matched_scenes": matched_scenes,
        "matched_boosts": matched_boosts,
    }


def _level_for_score(score: int, thresholds: dict, level_cap: str) -> str:
    """Map a score to a level, respecting the rule's level_cap."""
    critical_th = thresholds.get("critical", 85)
    important_th = thresholds.get("important", 65)

    if score >= critical_th and level_cap == "critical":
        return "critical"
    if score >= important_th and level_cap in ("important", "critical"):
        return "important"
    return "normal"


def _official_source_bonus(source_name: str, scoring: dict) -> int:
    official_names = [s.lower() for s in scoring.get("official_sources", [])]
    if source_name and source_name.lower() in official_names:
        return int(scoring.get("official_source_bonus", 5))
    return 0


def _category_bonus(category: str, scoring: dict) -> int:
    cat_map = scoring.get("category_bonus", {}) or {}
    if category and category.lower() in cat_map:
        return int(cat_map[category.lower()])
    return 0


def score_article(
    title: str,
    source_name: str,
    category: str,
    description: str,
    rules_config: dict,
) -> ImportanceResult:
    """Score a single article based on configured v2 rules."""

    if not rules_config.get("enabled", True):
        return ImportanceResult()

    rules = rules_config.get("rules", [])
    thresholds = rules_config.get("thresholds", {})
    scoring = rules_config.get("scoring", {}) or {}

    combined_text = f"{title} {description}".lower()

    matched_results = []
    for rule in rules:
        match = _rule_match(rule, combined_text)
        if match is None:
            continue

        score = int(rule.get("base_score", 0))
        reasons = []

        if match["matched_subjects"]:
            reasons.append("主体:" + ",".join(match["matched_subjects"][:3]))
        if match["matched_actions"]:
            reasons.append("动作:" + ",".join(match["matched_actions"][:3]))
        if match["matched_scenes"]:
            reasons.append("场景:" + ",".join(match["matched_scenes"][:3]))

        for boost in match["matched_boosts"]:
            score += int(boost.get("add", 0))
            reasons.append(
                "加强:" + ",".join(str(k) for k in boost.get("keywords", [])[:3])
            )

        official_bonus = _official_source_bonus(source_name, scoring)
        if official_bonus:
            score += official_bonus
            reasons.append("官方来源")

        cat_bonus = _category_bonus(category, scoring)
        if cat_bonus:
            score += cat_bonus
            reasons.append(f"分类:{category}")

        matched_results.append(
            {
                "rule_id": rule.get("id", "?"),
                "track": rule.get("track", "politics_security"),
                "score": score,
                "level_cap": rule.get("level_cap", "critical"),
                "reasons": reasons,
            }
        )

    if not matched_results:
        return ImportanceResult()

    if len(matched_results) >= 2:
        multi_bonus = int(scoring.get("multi_rule_bonus", 5))
        for r in matched_results:
            r["score"] += multi_bonus
            r["reasons"].append("多规则印证")

    for r in matched_results:
        r["score"] = max(0, min(100, r["score"]))
        r["level"] = _level_for_score(r["score"], thresholds, r["level_cap"])

    best = max(
        matched_results,
        key=lambda r: (LEVEL_ORDER[r["level"]], r["score"]),
    )

    return ImportanceResult(
        score=best["score"],
        level=best["level"],
        matched_rules=[r["rule_id"] for r in matched_results],
        reasons=best["reasons"],
        track=best["track"],
        matched_tracks=sorted({r["track"] for r in matched_results}),
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


def finalize_importance(
    importance_results: list,
    rules_config: dict,
) -> list:
    """Enforce the highlight cap and lane guarantees.

    Keeps at most ``total_cap`` articles at critical/important level. When any
    election-track candidate exists, at least ``lanes.election.min_slots`` of
    them are kept. All other candidates are downgraded to normal (their score
    and reasons are preserved and ``capped`` is set to True).

    Returns the same list (in the same order) with mutated results, so callers
    such as Word digest and Feishu card see one consistent view.
    """
    total_cap = int(rules_config.get("total_cap", 5) or 5)
    if total_cap < 1:
        total_cap = 1

    lanes = rules_config.get("lanes", {}) or {}
    election_lane = lanes.get("election", {}) or {}
    min_slots = int(election_lane.get("min_slots", 1) or 0)

    candidates = []
    for idx, (article, result) in enumerate(importance_results):
        if result.level in ("critical", "important"):
            candidates.append((idx, article, result))

    if not candidates:
        return importance_results

    def _sort_key(item: tuple) -> tuple:
        _idx, article, result = item
        published = article.published_at if article is not None else None
        pub_ts = published.timestamp() if published else 0
        return (LEVEL_ORDER[result.level], -result.score, -pub_ts)

    ordered = sorted(candidates, key=_sort_key)
    selected_idx: set[int] = set()
    selected: list = []

    # Election lane guarantee
    election_picks = [c for c in ordered if c[2].track == "election"][:min_slots]
    for c in election_picks:
        selected.append(c)
        selected_idx.add(c[0])

    # Global fill up to total_cap
    for c in ordered:
        if len(selected) >= total_cap:
            break
        if c[0] in selected_idx:
            continue
        selected.append(c)
        selected_idx.add(c[0])

    # Downgrade candidates that did not make the cut
    for idx, (_article, result) in enumerate(importance_results):
        if result.level in ("critical", "important") and idx not in selected_idx:
            result.level = "normal"
            result.capped = True

    return importance_results


def importance_summary(results: list[ImportanceResult]) -> str:
    """Generate a one-line summary of classification results."""
    counts = {"critical": 0, "important": 0, "normal": 0}
    for _article, r in results:
        counts[r.level] = counts.get(r.level, 0) + 1
    return (
        f"Importance summary: "
        f"critical={counts['critical']} "
        f"important={counts['important']} "
        f"normal={counts['normal']}"
    )


def validate_rules_config(config: dict) -> list[str]:
    """Validate the v2 importance rules configuration.

    Returns a list of human-readable errors (empty when valid).
    """
    errors = []
    if not isinstance(config.get("enabled"), bool):
        errors.append("enabled must be a boolean")
    if not config.get("enabled", True):
        return errors

    th = config.get("thresholds", {})
    for level in ("critical", "important", "normal"):
        val = th.get(level)
        if not isinstance(val, int):
            errors.append(f"thresholds.{level} must be an integer")
    if (
        isinstance(th.get("critical"), int)
        and isinstance(th.get("important"), int)
        and th.get("critical", 0) <= th.get("important", 0)
    ):
        errors.append("critical threshold must be > important threshold")
    if (
        isinstance(th.get("important"), int)
        and isinstance(th.get("normal"), int)
        and th.get("important", 0) <= th.get("normal", 0)
    ):
        errors.append("important threshold must be > normal threshold")

    display = config.get("display", {}) or {}
    if display:
        max_h = display.get("max_highlights")
        if not isinstance(max_h, int) or max_h < 1:
            errors.append("display.max_highlights must be an integer >= 1")

    total_cap = config.get("total_cap", 5)
    if not isinstance(total_cap, int) or total_cap < 1:
        errors.append("total_cap must be an integer >= 1")

    lanes = config.get("lanes", {}) or {}
    election_lane = lanes.get("election", {}) or {}
    if election_lane:
        min_slots = election_lane.get("min_slots")
        if not isinstance(min_slots, int) or min_slots < 0:
            errors.append("lanes.election.min_slots must be an integer >= 0")

    scoring = config.get("scoring", {}) or {}
    if scoring:
        for key in ("official_source_bonus", "multi_rule_bonus"):
            if key in scoring and (
                not isinstance(scoring[key], int) or scoring[key] < 0
            ):
                errors.append(f"scoring.{key} must be an integer >= 0")
        if "official_sources" in scoring and not isinstance(
            scoring["official_sources"], list
        ):
            errors.append("scoring.official_sources must be a list")
        if "category_bonus" in scoring and not isinstance(
            scoring["category_bonus"], dict
        ):
            errors.append("scoring.category_bonus must be a dict")

    rules = config.get("rules", [])
    rule_ids = set()
    for i, rule in enumerate(rules):
        rid = rule.get("id", f"<rule {i}>")
        if rid in rule_ids:
            errors.append(f"duplicate rule id: {rid}")
        rule_ids.add(rid)

        if rule.get("track") not in ALLOWED_TRACKS:
            errors.append(
                f"rule {rid}: track must be one of {list(ALLOWED_TRACKS)}"
            )

        base = rule.get("base_score")
        if not isinstance(base, int) or not (0 <= base <= 100):
            errors.append(
                f"rule {rid}: base_score must be an integer between 0 and 100"
            )

        if rule.get("level_cap") not in ALLOWED_LEVEL_CAPS:
            errors.append(
                f"rule {rid}: level_cap must be one of {list(ALLOWED_LEVEL_CAPS)}"
            )

        has_content = any(rule.get(k) for k in ("subjects", "actions", "scenes"))
        if not has_content:
            errors.append(
                f"rule {rid}: at least one of subjects/actions/scenes is required"
            )

        if "dimensions" in rule:
            errors.append(
                f"rule {rid}: dimensions is no longer supported; "
                "use base_score and boosts"
            )

        boosts = rule.get("boosts", [])
        if boosts is not None:
            if not isinstance(boosts, list):
                errors.append(f"rule {rid}: boosts must be a list")
            else:
                for j, b in enumerate(boosts):
                    if (
                        not isinstance(b, dict)
                        or not isinstance(b.get("keywords"), list)
                        or not isinstance(b.get("add"), int)
                    ):
                        errors.append(
                            f"rule {rid}: boosts[{j}] must have "
                            "keywords (list) and add (int)"
                        )

    return errors


def select_highlights(
    importance_results: list,
    max_highlights: int = 10,
    default_published_at: datetime | None = None,
) -> list:
    """Select and sort highlight articles from finalized importance results.

    Filters for critical and important articles, sorted by:
    - level: critical before important
    - score: descending
    - published_at: descending (newest first, None last)

    Returns a list of (article, ImportanceResult) tuples limited to
    max_highlights.
    """
    highlights = [
        (a, r) for a, r in importance_results
        if r.level in ("critical", "important")
    ]

    def _sort_key(item: tuple) -> tuple:
        article, result = item
        level_order = LEVEL_ORDER[result.level]
        score_neg = -result.score
        published = article.published_at if article.published_at else default_published_at
        pub_ts = published.timestamp() if published else 0
        pub_neg = -pub_ts
        return (level_order, score_neg, pub_neg)

    highlights.sort(key=_sort_key)

    return highlights[:max_highlights]
