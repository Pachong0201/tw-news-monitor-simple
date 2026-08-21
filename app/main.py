import argparse
import importlib
import logging
import sys
import os
import tempfile
import shutil
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
import json

import yaml

from .collectors import RSSCollector, UDNCollector, EBCCollector, CNAHtmlCollector, LtnRSSCollector, PresidentCollector, ZaobaoCollector, ReutersCollector, FTAlphavilleCollector, WSJRSSCollector, WSJNewsletterCollector, BloombergNewsletterCollector
from .category_classifier import apply_content_classification
from .content_filter import load_content_filter, filter_articles
from .database import Database
from .digest import build_digest
from .lock import InstanceLock
from .notifier import NullNotifier, create_notifier
from .feishu import build_highlight_card, send_card, send_document
from .word_digest import build_word_digest
from .summarizer import enrich_articles_with_summaries
from .freshness import FreshnessResult, filter_fresh_articles
from .source_registry import is_official_source, get_source_info, get_official_sources
from .time_utils import TAIPEI
from .importance import (
    classify_articles,
    finalize_importance,
    importance_summary,
    load_rules,
    select_highlights,
    validate_rules_config,
)
from .article_identity import article_identity_key, deduplicate_articles_by_identity
from .international import (
    dedupe_international_for_digest,
    filter_international,
    is_international_media,
    load_international_config,
)
from .international_events import cluster_international_articles
from .international_translation import (
    SummarizerTranslator,
    TranslationResult,
    translate_article,
)
from .notification_candidates import (
    NotificationDedupStore,
    build_notification_candidates,
)
from .source_health import SourceHealthStore, SourceOutcome

logger = logging.getLogger(__name__)

COLLECTOR_MAP = {
    "rss": RSSCollector,
    "udn": UDNCollector,
    "ebc": EBCCollector,
    "cna_list_html": CNAHtmlCollector,
    "zaobao": ZaobaoCollector,
    "ltn_rss": LtnRSSCollector,
    "newtalk_rss": RSSCollector,
    "president_json": PresidentCollector,
    "reuters": ReutersCollector,
    "ft_alphaville": FTAlphavilleCollector,
    "wsj_rss": WSJRSSCollector,
    "wsj_newsletter": WSJNewsletterCollector,
    "bloomberg_newsletter": BloombergNewsletterCollector,
}

_INTERNATIONAL_SOURCE_TYPES = {
    "reuters_international": "reuters",
    "ft_alphaville": "ft_alphaville",
    "wsj_newsletter": "wsj_newsletter",
    "bloomberg_newsletter": "bloomberg_newsletter",
}
_FROZEN_WSJ_SOURCE_ID = "wsj_international"
_FROZEN_WSJ_SOURCE_TYPE = "wsj_rss"
_NEWSLETTER_SOURCE_TYPES = frozenset({"wsj_newsletter", "bloomberg_newsletter"})


def setup_logging(log_path: Path, level: str = "INFO") -> None:
    """Configure rotating file logger. Console output stays as print()."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    fh = RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    fh.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s"
    ))
    root.addHandler(fh)


def load_sources(config_path: str | Path) -> list[dict]:
    try:
        with open(config_path, encoding="utf-8") as f:
            payload = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as exc:
        print(f"FATAL: unable to load source config: {exc}")
        raise SystemExit(1)
    if not isinstance(payload, dict) or not isinstance(payload.get("sources"), list):
        print("FATAL: source config must contain top-level sources list")
        raise SystemExit(1)
    if not all(isinstance(item, dict) for item in payload["sources"]):
        print("FATAL: every source entry must be a mapping")
        raise SystemExit(1)
    return payload["sources"]


def validate_sources_config(sources: list[dict], collector_map: dict) -> None:
    """Validate source config before any network/DB/Word/Feishu operations.

    Rules: id non-empty + unique, type non-empty + in map, enabled is real bool,
    url non-empty, category valid, collector-only without type errors.
    Exits with code 1 on any error.
    """
    VALID_CATEGORIES = {
        "politics",
        "economy",
        "military",
        "international",
        "religion",
    }
    errors = []
    seen_ids = set()

    if not isinstance(sources, list):
        print("FATAL: source config must be a list")
        raise SystemExit(1)
    if not isinstance(collector_map, dict):
        print("FATAL: collector map must be a mapping")
        raise SystemExit(1)

    for i, source in enumerate(sources):
        if not isinstance(source, dict):
            errors.append(f"[<index {i}>] source entry must be a mapping")
            continue
        sid = source.get("id", f"<index {i}>")

        if not isinstance(source.get("id"), str) or not source.get("id", "").strip():
            errors.append(f"[<index {i}>] id is empty or missing")
            continue

        if sid in seen_ids:
            errors.append(f"[{sid}] duplicate source id")
        seen_ids.add(sid)

        stype = source.get("type")
        if not isinstance(stype, str) or not stype.strip():
            has_collector = "collector" in source
            if has_collector:
                errors.append(
                    f"[{sid}] has 'collector' field but no 'type' field; "
                    f"add type or remove collector"
                )
            else:
                errors.append(f"[{sid}] 'type' field is missing or empty")
            continue

        if stype not in collector_map:
            errors.append(f"[{sid}] type='{stype}' not found in COLLECTOR_MAP")

        url = source.get("url")
        if not url or not isinstance(url, str):
            errors.append(f"[{sid}] 'url' is missing or empty")

        cat = source.get("category") or source.get("default_category")
        if cat and (not isinstance(cat, str) or cat not in VALID_CATEGORIES):
            allowed = ", ".join(sorted(VALID_CATEGORIES))
            errors.append(f"[{sid}] category='{cat}' is not valid (allowed: {allowed})")

        if "enabled" in source:
            enabled_val = source["enabled"]
            if not isinstance(enabled_val, bool):
                errors.append(
                    f"[{sid}] enabled={repr(enabled_val)} must be a boolean "
                    f"(True/False), not string"
                )

    # Once an international source is present, the production configuration
    # must contain exactly one entry for every planned source. Generic test
    # configurations without any international IDs retain the historical
    # validator behavior, while partial/malformed international configs fail
    # closed before any DB/network work.
    source_ids = [
        source.get("id")
        for source in sources
        if isinstance(source, dict) and isinstance(source.get("id"), str)
    ]
    strict_international = bool(
        set(source_ids) & (set(_INTERNATIONAL_SOURCE_TYPES) | {_FROZEN_WSJ_SOURCE_ID})
        or any(
            isinstance(source, dict)
            and source.get("type") == _FROZEN_WSJ_SOURCE_TYPE
            for source in sources
        )
    )
    if strict_international:
        by_id = {}
        for source in sources:
            if isinstance(source, dict) and source.get("id"):
                by_id.setdefault(source["id"], []).append(source)
        for source_id, expected_type in _INTERNATIONAL_SOURCE_TYPES.items():
            entries = by_id.get(source_id, [])
            if len(entries) != 1:
                errors.append(
                    f"[{source_id}] must appear exactly once in international config"
                )
                continue
            source = entries[0]
            if source.get("type") != expected_type:
                errors.append(f"[{source_id}] type must be {expected_type!r}")
            if type(source.get("enabled")) is not bool:
                errors.append(f"[{source_id}] enabled must be an independent boolean")
            if source_id in _NEWSLETTER_SOURCE_TYPES:
                if source.get("access_level") != "newsletter":
                    errors.append(f"[{source_id}] access_level must be 'newsletter'")
                if source.get("mailbox_label") != "InternationalNews":
                    errors.append(
                        f"[{source_id}] mailbox_label must be 'InternationalNews'"
                    )
                for field in ("sender_allowlist", "article_allowlist"):
                    value = source.get(field)
                    if not isinstance(value, (list, tuple, set, frozenset)) or not value:
                        errors.append(f"[{source_id}] {field} must be a non-empty allowlist")

        frozen = by_id.get(_FROZEN_WSJ_SOURCE_ID, [])
        if len(frozen) != 1:
            errors.append(
                f"[{_FROZEN_WSJ_SOURCE_ID}] frozen WSJ RSS entry must appear exactly once"
            )
        elif frozen[0].get("type") != _FROZEN_WSJ_SOURCE_TYPE:
            errors.append(
                f"[{_FROZEN_WSJ_SOURCE_ID}] frozen WSJ RSS type must be {_FROZEN_WSJ_SOURCE_TYPE!r}"
            )
        elif frozen[0].get("enabled") is not False:
            errors.append(f"[{_FROZEN_WSJ_SOURCE_ID}] frozen WSJ RSS must remain disabled")

    if errors:
        print("=" * 60)
        print("FATAL: Source config validation failed")
        print("=" * 60)
        for err in errors:
            print(f"  ERROR: {err}")
        print(f"\n{len(errors)} configuration error(s) found. Exiting.")
        sys.exit(1)

    logger.info("Source config validation passed (%d sources)", len(sources))



def deduplicate_articles_by_url(articles):
    """Deduplicate by normalized URL within the same run.

    Keeps first occurrence, maintains order. O(n) time.
    Returns (unique_articles, duplicate_articles).
    """
    seen = set()
    unique = []
    dups = []
    for article in articles:
        url = article.url
        if not url:
            unique.append(article)
            continue
        if url in seen:
            dups.append(article)
        else:
            seen.add(url)
            unique.append(article)
    return unique, dups


def enrich_summaries_safe(articles, db=None) -> None:
    """Best-effort summary enrichment; never blocks Word generation."""
    try:
        enrich_articles_with_summaries(articles, db)
    except Exception as exc:
        logger.warning("Summary enrichment failed: %s", exc)


class _PrecomputedTranslationLookup:
    """Metadata-only translator view for event notification rendering.

    ``notification_candidates`` intentionally accepts the small translator
    protocol rather than a URL-aware object.  This lookup bridges that
    protocol to the results already computed for Word, so an event candidate
    never invokes a second provider call or invents a different translation.
    Fallback entries are excluded: the notification then uses the safe
    English metadata fallback from the shared delivery code.
    """

    def __init__(self, articles: list, translations: dict[str, TranslationResult]):
        self._by_metadata = {}
        for article in articles:
            result = translations.get(getattr(article, "url", ""))
            if result is None or result.status != "translated":
                continue
            key = self._key(article.title, article.summary, article.source_name)
            self._by_metadata[key] = result

    @staticmethod
    def _key(title: str, summary: str | None, source_name: str) -> tuple[str, str, str]:
        return (
            str(title or "").strip(),
            str(summary or "").strip(),
            str(source_name or "").strip(),
        )

    def translate(self, title: str, summary: str | None, *, source_name: str):
        key = self._key(title, summary, source_name)
        result = self._by_metadata.get(key)
        if result is None:
            raise LookupError("no precomputed translated metadata for article")
        if isinstance(result, tuple):
            return result
        return result.cn_title, result.cn_summary


def _build_notification_translation_lookup(
    articles: list,
    translations: dict[str, TranslationResult],
    international_coverage: dict[str, list] | None,
) -> _PrecomputedTranslationLookup | None:
    """Make event evidence render the canonical translation/fallback."""

    if not articles:
        return None
    lookup = _PrecomputedTranslationLookup(articles, translations)
    by_url = {str(getattr(article, "url", "")): article for article in articles}
    for canonical_url, members in (international_coverage or {}).items():
        canonical = by_url.get(str(canonical_url))
        values = list(members or [])
        if canonical is None and values:
            canonical = values[0]
        if canonical is None:
            continue
        canonical_result = translations.get(str(getattr(canonical, "url", "")))
        if canonical_result is not None and canonical_result.status == "translated":
            rendered = (canonical_result.cn_title, canonical_result.cn_summary)
        else:
            # This is intentionally English metadata, not a synthetic
            # translation.  It keeps the candidate and Word canonical in sync.
            rendered = (
                str(getattr(canonical, "title", "") or ""),
                str(getattr(canonical, "summary", "") or "metadata-only fallback"),
            )
        for member in values:
            lookup._by_metadata[lookup._key(
                member.title, member.summary, member.source_name
            )] = rendered
    return lookup


def _build_international_translator():
    """Build an optional production translator adapter, never a test fake.

    The existing summarizer module currently exposes summary enrichment but no
    stable title+summary translation callable.  A deployment may provide one
    through ``INTERNATIONAL_TRANSLATOR_FACTORY=module:function``; when it is
    absent (the normal current state), callers receive the strict English
    metadata fallback.  This keeps the pipeline explicit and makes a future
    summarizer implementation a drop-in adapter without coupling delivery to
    a provider or fetching an article body.
    """

    spec = os.getenv("INTERNATIONAL_TRANSLATOR_FACTORY", "").strip()
    try:
        if spec:
            module_name, function_name = spec.rsplit(":", 1)
            factory = getattr(importlib.import_module(module_name), function_name)
            provider = factory() if callable(factory) else factory
        else:
            # The current summarizer has no translation provider, but this
            # capability probe makes the adapter explicit when a deployment
            # supplies one without changing the delivery pipeline.
            from . import summarizer as summarizer_module
            provider = (
                getattr(summarizer_module, "translate_metadata", None)
                or getattr(summarizer_module, "international_translator", None)
            )
        if provider is None:
            return None
        return SummarizerTranslator(provider)
    except Exception as exc:
        logger.warning("International translator unavailable; using metadata fallback: %s", exc)
        return None


def _precompute_international_translations(
    articles: list,
    international_config: dict | None,
    translator=None,
) -> dict[str, TranslationResult]:
    """Translate only international delivery articles from legal metadata."""

    if not international_config or not international_config.get("enabled", False):
        return {}
    result: dict[str, TranslationResult] = {}
    for article in articles:
        if not is_international_media(article.source_name, international_config):
            continue
        # ``translate_article`` intentionally ignores body_fetcher and only
        # sees Article title/teaser.  With no real provider it records the
        # explicit English metadata fallback.
        result[article.url] = translate_article(article, translator=translator)
    return result


def _translation_articles_for_delivery(
    delivery_articles: list,
    international_coverage: dict[str, list] | None,
) -> list:
    """Return every current-run international member used by delivery.

    The visible Word set contains only canonical articles, while a fresh
    coverage member may be the notification evidence for an older canonical.
    Precomputing the union keeps both output paths on the same translator or
    the same strict English fallback.
    """

    result: list = []
    seen: set[str] = set()
    values = list(delivery_articles or [])
    for members in (international_coverage or {}).values():
        values.extend(members or [])
    for article in values:
        marker = str(getattr(article, "url", "") or "")
        if marker in seen:
            continue
        seen.add(marker)
        result.append(article)
    return result


def _notification_dedup_store(
    project_root: Path,
    international_config: dict | None,
    *,
    dry_run: bool = False,
) -> NotificationDedupStore | None:
    """Resolve an isolated event-dedup path; dry-run never writes it."""

    if dry_run:
        return None
    configured = os.getenv("INTERNATIONAL_NOTIFICATION_DEDUP_PATH", "").strip()
    if not configured and isinstance(international_config, dict):
        configured = str(
            international_config.get("notification_dedup_path")
            or (international_config.get("notification", {}) or {}).get("dedup_path")
            or ""
        ).strip()
    path = Path(configured) if configured else project_root / "data" / "international_notification_dedup.json"
    if not path.is_absolute():
        path = project_root / path
    return NotificationDedupStore(path)


def _deliver_event_candidates(
    notifier,
    candidates: list,
    dedup_store: NotificationDedupStore | None,
    now: datetime,
) -> bool:
    """Deliver candidates and persist only an explicit ``True`` result."""

    try:
        result = notifier.send_event_candidates(candidates)
    except Exception as exc:
        logger.warning("International event notifier raised: %s", exc)
        return False
    if result is not True:
        return False
    if dedup_store is not None:
        for candidate in candidates:
            dedup_store.mark_sent(candidate.dedup_key, now=now)
    return True


def _build_newsletter_mailbox(source: dict):
    """Build a readonly Gmail mailbox from external auth metadata only.

    No OAuth flow is started here.  Missing paths intentionally produce an
    unauthorized ``AuthContext`` and the injected newsletter collector then
    records ``MAILBOX_AUTH_REQUIRED`` as a source failure.
    """

    from .newsletter_ingestion.gmail_client import (
        GmailMailboxClient,
        build_service,
    )
    from .newsletter_ingestion.oauth import load_auth_context

    credentials_path = (
        os.getenv("GMAIL_CLIENT_SECRET_PATH")
        or os.getenv("GMAIL_CREDENTIALS_PATH")
        or os.getenv("GOOGLE_CLIENT_SECRET_PATH")
    )
    token_path = os.getenv("GMAIL_TOKEN_PATH") or os.getenv("GOOGLE_TOKEN_PATH")
    auth = load_auth_context(credentials_path, token_path)
    service = build_service(auth) if auth.authorized else None
    return GmailMailboxClient(
        service=service,
        label=source.get("mailbox_label", "InternationalNews"),
        modify=False,
        auth=auth,
    )


def _construct_collector(collector_cls, source: dict):
    """Construct a source collector, injecting mailbox policy for newsletters."""

    if source.get("type") in _NEWSLETTER_SOURCE_TYPES:
        return collector_cls(source, mailbox=_build_newsletter_mailbox(source))
    return collector_cls(source)


def collect_all(
    sources: list[dict], db: Database,
    content_filter_config: dict | None = None,
    *,
    collector_map: dict | None = None,
    health_store: SourceHealthStore | None = None,
) -> tuple[list, int, int, list[str]]:
    """Collect news, dedup by URL, save new ones.

    Returns (inserted, total_fetched, dup_count, failed_sources,
             run_removed, hist_id_dups, filtered_count).
    """
    all_raw: list = []
    total_fetched = 0
    failed: list[str] = []

    active_collector_map = collector_map or COLLECTOR_MAP
    for source in sources:
        cls = active_collector_map.get(source.get("type"))
        if not cls:
            logger.warning("Unknown collector: %s", source.get("type"))
            continue
        if source.get("enabled") is False:
            if health_store is not None:
                health_store.disable(source["id"])
            continue
        collector = None
        try:
            # Construction is inside the same isolation boundary as collection:
            # a bad optional dependency/config cannot abort Taiwan sources.
            collector = _construct_collector(cls, source)
            articles = collector.collect()
            outcome = getattr(collector, "last_outcome", None)
            if isinstance(outcome, SourceOutcome) and (
                outcome.error_code
                or outcome.schema_valid is not True
                or not 200 <= int(outcome.http_status) < 300
            ):
                failed.append(source["id"])
                if health_store is not None:
                    health_store.update(source["id"], outcome)
                error_message = getattr(collector, "last_error_message", None)
                error_message = error_message or outcome.error_code or "source outcome invalid"
                logger.error("Failed %s: %s", source["id"], error_message)
                print(f"  [ERR] {source['id']}: {error_message}")
                continue
            articles = apply_content_classification(articles, source)
            total_fetched += len(articles)
            if health_store is not None:
                if not isinstance(outcome, SourceOutcome):
                    outcome = SourceOutcome(200, True, len(articles))
                health_store.update(source["id"], outcome)
            logger.info("collected %s: fetched=%d", source["id"], len(articles))
            all_raw.extend(articles)
            print(f"  [OK] {source['id']}: fetched={len(articles)}")
        except Exception as e:
            failed.append(source["id"])
            if health_store is not None:
                outcome = getattr(collector, "last_outcome", None)
                if not isinstance(outcome, SourceOutcome):
                    outcome = SourceOutcome(0, False, 0, "config")
                try:
                    health_store.update(source["id"], outcome)
                except Exception:
                    logger.warning("Source health update failed for %s", source["id"])
            logger.error("Failed %s: %s", source["id"], e)
            print(f"  [ERR] {source['id']}: {e}")
        finally:
            if collector is not None:
                try:
                    collector.close()
                except Exception:
                    logger.warning("Collector close failed for %s", source["id"])

    # Phase 2: Intra-run URL dedup
    unique_by_url, run_dups = deduplicate_articles_by_url(all_raw)

    # Phase 2.5: Intra-run identity dedup (e.g. UDN alias)
    unique_articles, identity_run_dups = deduplicate_articles_by_identity(unique_by_url)
    logger.info(
        "Intra-run dedup: raw=%d, url_unique=%d, url_removed=%d, id_removed=%d",
        len(all_raw), len(unique_articles), len(run_dups), len(identity_run_dups),
    )

    # Phase 3: DB check with identity keys (prevents UDN alias re-insertion)
    existing_urls = set(db.get_all_article_urls())
    existing_ids = {article_identity_key(u) for u in existing_urls}
    candidates = []
    hist_url_dups = []
    hist_id_dups = []
    for a in unique_articles:
        if a.url in existing_urls:
            hist_url_dups.append(a)
        elif article_identity_key(a.url) in existing_ids:
            hist_id_dups.append(a)
        else:
            candidates.append(a)

    # Content filter: exclude out-of-scope news (e.g. social trivia in
    # economy feeds) before saving when mode=drop_before_save.
    filtered_count = 0
    if content_filter_config and content_filter_config.get("enabled", False):
        kept_candidates, blocked = filter_articles(candidates, content_filter_config)
        filtered_count = len(blocked)
        if blocked:
            logger.info(
                "Content filter blocked %d/%d candidates: %s",
                len(blocked), len(candidates),
                [a.title[:40] for a in blocked[:5]],
            )
        candidates = kept_candidates

    inserted = db.save_articles(candidates)

    run_removed = len(run_dups) + len(identity_run_dups)
    dup_count = total_fetched - len(inserted) - filtered_count
    logger.info(
        "Total: fetched=%d, run_url_removed=%d, run_id_removed=%d, "
        "hist_url_dup=%d, hist_id_dup=%d, filtered=%d, inserted=%d, failed=%d",
        total_fetched, len(run_dups), len(identity_run_dups),
        len(hist_url_dups), len(hist_id_dups),
        filtered_count, len(inserted), len(failed),
    )
    return (
        inserted, total_fetched, dup_count, failed,
        run_removed, len(hist_id_dups), filtered_count,
    )


def show_db_stats(db: Database) -> None:
    total = db.count_articles()
    print(f"数据库文章总数: {total}")
    print()
    by_source = db.count_by_source()
    if by_source:
        print("各来源数量:")
        for sid, cnt in sorted(by_source.items()):
            print(f"  {sid}: {cnt}")
    print()
    by_cat = db.count_by_category()
    if by_cat:
        print("各栏目数量:")
        for cat, cnt in sorted(by_cat.items()):
            print(f"  {cat}: {cnt}")


def _classify_delivery_articles(
    inserted_articles: list,
    source_baselines: dict[str, int],
    run_started_at: datetime,
    catch_up_enabled: bool = False,
    catch_up_max_minutes: int = 720,
):
    freshness = filter_fresh_articles(
        inserted_articles, run_started_at,
        catch_up_enabled=catch_up_enabled,
        catch_up_max_minutes=catch_up_max_minutes,
    )
    fresh_articles = freshness.fresh_articles
    catch_up_articles = freshness.catch_up_articles
    stale_articles = freshness.stale_articles
    unknown_articles = freshness.unknown_time_articles
    future_articles = freshness.future_time_articles
    catch_up_eligible = [
        a for a in catch_up_articles
        if source_baselines.get(a.source_id, 0) > 0
    ]
    baseline_excluded = [
        a for a in catch_up_articles
        if source_baselines.get(a.source_id, 0) == 0
    ]
    stale_articles = stale_articles + baseline_excluded
    catch_up_urls = {a.url for a in catch_up_eligible}
    return {
        'fresh_articles': fresh_articles,
        'catch_up_eligible': catch_up_eligible,
        'catch_up_urls': catch_up_urls,
        'stale_articles': stale_articles,
        'unknown_articles': unknown_articles,
        'future_articles': future_articles,
        'baseline_excluded': baseline_excluded,
    }


def _get_source_baselines(db: Database, sources: list[dict]) -> dict[str, int]:
    """Return per-source row counts before the current collection starts."""
    baselines: dict[str, int] = {}
    for source in sources:
        source_id = source["id"]
        try:
            baselines[source_id] = db.conn.execute(
                "SELECT COUNT(*) FROM articles WHERE source_id = ?", (source_id,)
            ).fetchone()[0]
        except Exception:
            baselines[source_id] = 0
    return baselines


def prepare_international_delivery(
    delivery_articles: list,
    international_config: dict | None,
) -> tuple[list, dict]:
    """国际媒体层（Phase I）：相关度过滤 + 跨媒体事件去重（显示层）。

    仅作用于“即将进入 digest/Word/高亮”的文章集合；DB 落库（collect_all 内
    完成）不受影响——所有国际媒体文章（含被判定不相关的）仍按 URL 去重后
    正常入库。非国际媒体文章始终原样通过。

    容错：层配置缺失或层内异常时回退为原样放行（与未启用行为一致），
    绝不中断主流程；单国际源采集失败由 collect_all 的逐源 try/except 隔离。

    返回 (digest_articles, coverage)：
    - digest_articles：进入简报的文章（国际媒体 canonical + 非国际媒体文章）；
    - coverage：以 canonical.url 为键的事件成员表（含 canonical 自身）。
    """
    cfg = international_config or {}
    if not cfg.get("enabled", False):
        return list(delivery_articles), {a.url: [a] for a in delivery_articles}
    try:
        included, excluded = filter_international(delivery_articles, cfg)
        if excluded:
            logger.info(
                "International layer excluded %d irrelevant article(s): %s",
                len(excluded), [a.title[:40] for a in excluded[:5]],
            )
        clusters, coverage = cluster_international_articles(included, cfg)
        canonical = [cluster.canonical for cluster in clusters]
        merged = sum(1 for members in coverage.values() if len(members) > 1)
        logger.info(
            "International layer: delivered=%d (merged events=%d)",
            len(canonical), merged,
        )
        return canonical, coverage
    except Exception as exc:
        logger.warning(
            "International layer failed, delivering unprocessed articles: %s", exc
        )
        return list(delivery_articles), {}

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Taiwan News Monitor - Simple Edition"
    )
    parser.add_argument(
        "--bootstrap", action="store_true",
        help="Initial collect + save, no notification sent",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Collect and print, no save or notify",
    )
    parser.add_argument(
        "--test-notify", action="store_true",
        help="Send test notification",
    )
    parser.add_argument(
        "--db-stats", action="store_true",
        help="Show database statistics",
    )
    parser.add_argument(
        "--export-word", nargs="?", const=30, type=int, default=None,
        metavar="N",
        help="Export latest N articles to Word (default: 30)",
    )
    parser.add_argument(
        "--backfill-run", type=str, default=None,
        metavar="BATCH_ID",
        help="Catch-up delivery for previously inserted but unpushed articles",
    )
    parser.add_argument(
        "--send", action="store_true",
        help="Actually send when used with --backfill-run",
    )
    parser.add_argument(
        "--list-feishu-chats", action="store_true",
        help="List Feishu group chats the bot has joined",
    )
    parser.add_argument(
        "--test-feishu-app", action="store_true",
        help="Send a test text message via Feishu App bot",
    )
    parser.add_argument(
        "--test-feishu-file", action="store_true",
        help="Send a test Word file to Feishu chat",
    )
    parser.add_argument(
        "--diagnose-collection", action="store_true",
        help="Collect and diagnose duplicates and time issues (READ-ONLY)",
    )
    parser.add_argument(
        "--diagnose-source", type=str, default=None,
        metavar="SOURCE_ID",
        help="Diagnose a specific source by ID (read-only, no DB write)",
    )
    parser.add_argument(
        "--diagnose-file", type=str, default=None,
        metavar="PATH",
        help="Replay diagnosis from saved JSON (no network)",
    )
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    config_path_str = os.getenv("SOURCES_CONFIG_PATH", "")
    if config_path_str:
        config_path = Path(config_path_str)
    else:
        config_path = project_root / "config" / "sources.yaml"
    sources = load_sources(config_path)
    validate_sources_config(sources, COLLECTOR_MAP)
    importance_rules_path = project_root / 'config' / 'importance_rules.yaml'
    if importance_rules_path.exists():
        importance_rules_config = load_rules(importance_rules_path)
    else:
        importance_rules_config = {'enabled': False, 'thresholds': {}, 'rules': []}
    importance_errors = validate_rules_config(importance_rules_config)
    if importance_errors:
        msg = "重要度规则配置无效：" + "；".join(importance_errors)
        logger.error(msg)
        print(msg)
        return
    content_filter_config = load_content_filter(
        project_root / "config" / "content_filter.yaml"
    )
    if content_filter_config.get("enabled", False):
        logger.info(
            "Content filter enabled (mode=%s)",
            content_filter_config.get("mode", "drop_before_save"),
        )
    international_config = load_international_config(
        project_root / "config" / "international_media.yaml"
    )
    if international_config.get("enabled", False):
        logger.info("International media layer enabled")
    else:
        logger.info("International media layer disabled (config missing or disabled)")
    db_path_str = os.getenv("NEWS_DB_PATH", "")
    if db_path_str:
        db_path = Path(db_path_str)
    else:
        db_path = project_root / "data" / "news.db"
    health_path = Path(os.getenv("INTERNATIONAL_SOURCE_HEALTH_PATH", "")) if os.getenv("INTERNATIONAL_SOURCE_HEALTH_PATH", "").strip() else project_root / "data" / "international_source_health.json"
    health_store = SourceHealthStore(health_path)

    # Setup file logging (console stays as print())
    setup_logging(project_root / "data" / "monitor.log")
    logger.info("=" * 50)
    logger.info("Taiwan News Monitor started")
    logger.info(
        "Args: %s",
        " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "(default)",
    )

    # ---- Bootstrap ----------------------------------------------------
    if args.bootstrap:
        db = Database(db_path)
        db.connect()
        db.create_tables()
        inserted, total, dup, failed, run_removed, hist_id_dup, filtered_count = collect_all(
            sources, db, content_filter_config, health_store=health_store
        )
        print()
        print("初始化完成：")
        print(f"  本轮采集：{total}条")
        print(f"  新增入库：{len(inserted)}条")
        print(f"  重复跳过：{dup}条")
        if filtered_count:
            print(f"  内容过滤：{filtered_count}条")
        if failed:
            print(f"\n失败来源：{len(failed)}个（{', '.join(failed)}）")
        else:
            print("  失败来源：0个")
        logger.info(
            "Bootstrap done: total=%d, new=%d, dup=%d, filtered=%d, failed=%d",
            total, len(inserted), dup, filtered_count, len(failed),
        )
        db.close()
        logger.info("Taiwan News Monitor ended")
        return

    # ---- Test Notify --------------------------------------------------
    if args.test_notify:
        notifier = create_notifier()
        test_msg = (
            "【台湾新闻监测 - 测试消息】\n\n"
            "这是一条测试通知，用于验证通知渠道是否正常。\n"
            f"发送时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        notifier.send(test_msg)
        print("测试通知已发送。")
        return

    # ---- DB Stats -----------------------------------------------------
    if args.db_stats:
        if not db_path.exists():
            print("数据库不存在，请先运行 python -m app.main --bootstrap")
            return
        db = Database(db_path)
        db.connect()
        show_db_stats(db)
        db.close()
        return


    # ---- List Feishu Chats --------------------------------------------
    if args.list_feishu_chats:

        from dotenv import load_dotenv
        load_dotenv()
        app_id = os.getenv("FEISHU_APP_ID", "").strip()
        app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
        if not app_id or not app_secret:
            print("\u8bf7\u8bbe\u7f6e FEISHU_APP_ID \u548c FEISHU_APP_SECRET \u73af\u5883\u53d8\u91cf\u3002")
            return
        try:
            from .feishu import list_bot_chats
            chats = list_bot_chats(app_id, app_secret)
            if not chats:
                print("\u5f53\u524d\u5e94\u7528\u673a\u5668\u4eba\u5c1a\u672a\u52a0\u5165\u4efb\u4f55\u7fa4\u804a\u3002")
                return
            print("\u673a\u5668\u4eba\u6240\u5728\u7fa4\u804a\uff1a\n")
            for i, chat in enumerate(chats, 1):
                name = chat.get("name", "(\u672a\u547d\u540d)")
                chat_id = chat.get("chat_id", "")
                print(f"{i}. {name}")
                print(f"   chat_id: {chat_id}")
                print()
        except RuntimeError as e:
            print(f"\u83b7\u53d6\u7fa4\u804a\u5217\u8868\u5931\u8d25: {e}")
        return


    # ---- Test Feishu App ----------------------------------------------
    if args.test_feishu_app:

        from dotenv import load_dotenv
        load_dotenv()
        app_id = os.getenv("FEISHU_APP_ID", "").strip()
        app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
        chat_id = os.getenv("FEISHU_CHAT_ID", "").strip()
        missing = []
        if not app_id:
            missing.append("FEISHU_APP_ID")
        if not app_secret:
            missing.append("FEISHU_APP_SECRET")
        if not chat_id:
            missing.append("FEISHU_CHAT_ID")
        if missing:
            print(f"\u8bf7\u8bbe\u7f6e\u73af\u5883\u53d8\u91cf: {', '.join(missing)}")
            return
        try:
            from .feishu import send_text
            send_text("\u53f0\u6e7e\u65b0\u95fb\u76d1\u6d4b\u673a\u5668\u4eba\u8fde\u63a5\u6d4b\u8bd5\u6210\u529f", app_id, app_secret, chat_id)
            print("\u6d4b\u8bd5\u6d88\u606f\u5df2\u53d1\u9001\uff0c\u8bf7\u68c0\u67e5\u624b\u673a\u98de\u4e66\u7fa4\u3002")
        except RuntimeError as e:
            print(f"\u53d1\u9001\u5931\u8d25: {e}")
        return

    # ---- Test Feishu File ---------------------------------------------
    if args.test_feishu_file:

        from dotenv import load_dotenv
        load_dotenv()
        app_id = os.getenv("FEISHU_APP_ID", "").strip()
        app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
        chat_id = os.getenv("FEISHU_CHAT_ID", "").strip()
        missing = []
        if not app_id: missing.append("FEISHU_APP_ID")
        if not app_secret: missing.append("FEISHU_APP_SECRET")
        if not chat_id: missing.append("FEISHU_CHAT_ID")
        if missing:
            print(f"\u8bf7\u8bbe\u7f6e\u73af\u5883\u53d8\u91cf: {', '.join(missing)}")
            return
        db_path = project_root / "data" / "news.db"
        if not db_path.exists():
            print("\u6570\u636e\u5e93\u4e0d\u5b58\u5728\uff0c\u8bf7\u5148\u8fd0\u884c python -m app.main --bootstrap")
            return
        db = Database(db_path)
        db.connect()
        now = datetime.now(TAIPEI)
        articles = db.get_articles_since(datetime(2000, 1, 1))
        articles = articles[-10:] if len(articles) > 10 else articles
        if not articles:
            print("\u6570\u636e\u5e93\u4e2d\u6682\u65e0\u65b0\u95fb\u3002")
            db.close()
            return
        output_dir = project_root / "data" / "reports"
        enrich_summaries_safe(articles, db)
        output_path = build_word_digest(articles, output_dir, generated_at=now)
        test_name = f"\u53f0\u6e7e\u65b0\u95fb\u76d1\u6d4b_\u6d4b\u8bd5_{now.strftime('%Y-%m-%d_%H%M')}.docx"
        test_path = output_path.parent / test_name
        if test_path.exists():
            test_path.unlink()
        output_path.rename(test_path)
        try:
            caption = "\u3010\u53f0\u6e7e\u65b0\u95fb\u76d1\u6d4b\uff5cWord\u9644\u4ef6\u6d4b\u8bd5\u3011"
            send_document(test_path, app_id, app_secret, chat_id, caption=caption)
            print(f"Word\u9644\u4ef6\u6d4b\u8bd5\u5b8c\u6210\u3002" + "\n" + f"\u6587\u4ef6\uff1a{test_path}")
            logger.info("Feishu file test complete: %s", test_path)
        except Exception as e:
            print(f"\u53d1\u9001\u5931\u8d25: {e}" + "\n" + f"\u672c\u5730\u6587\u4ef6\u4ecd\u4fdd\u7559: {test_path}")
            logger.warning("Feishu file test failed: %s", e)
        db.close()
        return

    # ---- Diagnose Collection ------------------------------------------
    if args.diagnose_collection:

        from dotenv import load_dotenv
        load_dotenv()
        dbp = os.getenv("DATABASE_PATH", "data/news-dev.db")
        diag_db = project_root / dbp
        if not diag_db.exists():
            print(f"诊断数据库不存在: {diag_db}")
            print("请将备份数据库复制到 data/news-dev.db 或设置 DATABASE_PATH")
            return
        db_obj = Database(diag_db)
        db_obj.connect()
        print()
        print("运行环境：development")
        print(f"数据库：{dbp}")
        print("通知渠道：console")
        print("飞书发送：已禁用")
        print()
        out_dir = project_root / "data" / "diagnostics"
        from .diagnose import run_diagnosis
        run_diagnosis(sources, db_obj, out_dir)
        print()
        print(f"诊断CSV: {out_dir / 'latest_collection.csv'}")
        print(f"诊断报告: {out_dir / 'latest_diagnosis.md'}")
        db_obj.close()
        return

    # ---- Dry Run ------------------------------------------------------
    if args.dry_run:
        # Dry-run is a full international delivery rehearsal, but all state
        # is isolated under a disposable temporary workspace and notifications
        # are no-op.  In particular it must not read/write production DB,
        # reports, health or notification-dedup state.
        dry_root = Path(tempfile.mkdtemp(prefix="tw-news-monitor-dry-run-"))
        tmp_path = dry_root / "news.db"
        tmp_reports = dry_root / "reports"
        tmp_health = SourceHealthStore(dry_root / "international_source_health.json")
        db = Database(tmp_path)
        db.connect()
        try:
            db.create_tables()
            source_baselines = _get_source_baselines(db, sources)
            inserted, total, dup, failed, run_removed, hist_id_dup, filtered_count = collect_all(
                sources, db, content_filter_config, health_store=tmp_health
            )
            now = datetime.now(TAIPEI)
            classification = _classify_delivery_articles(
                inserted, source_baselines, now,
                catch_up_enabled=False,
            )
            fresh_articles = classification["fresh_articles"]
            delivery_articles = fresh_articles
            digest_articles, intl_coverage = prepare_international_delivery(
                delivery_articles, international_config
            )
            enrich_summaries_safe(digest_articles, db)
            importance_results = finalize_importance(
                classify_articles(digest_articles, importance_rules_config),
                importance_rules_config,
            )
            translator = _build_international_translator()
            translation_articles = _translation_articles_for_delivery(
                delivery_articles, intl_coverage
            )
            international_translations = _precompute_international_translations(
                translation_articles, international_config, translator=translator
            )
            notification_translator = _build_notification_translation_lookup(
                translation_articles, international_translations, intl_coverage
            )
            event_candidates = []
            try:
                relevant_delivery, _ = filter_international(
                    delivery_articles, international_config or {}
                )
                event_clusters, _ = cluster_international_articles(
                    relevant_delivery, international_config or {}
                )
                event_candidates = build_notification_candidates(
                    event_clusters,
                    importance_results,
                    {
                        "fresh_articles": fresh_articles,
                        "catch_up_urls": set(),
                        "baseline_excluded": classification["baseline_excluded"],
                        "stale_articles": classification["stale_articles"],
                        "unknown_articles": classification["unknown_articles"],
                        "future_articles": classification["future_articles"],
                    },
                    now,
                    translator=notification_translator,
                    dedup_store=None,
                )
                NullNotifier().send_event_candidates(event_candidates)
            except Exception as event_err:
                logger.warning("Dry-run international event path failed safely: %s", event_err)

            long_digest_articles = [
                article for article in digest_articles
                if not is_international_media(article.source_name, international_config)
            ]
            if long_digest_articles:
                digest = build_digest(
                    long_digest_articles, now,
                    international_coverage=intl_coverage,
                    international_config=international_config,
                    include_international_media=False,
                )
            else:
                digest = build_digest([], now)
            digest += (
                f"\n本轮收集：{total}条\n"
                f"新增入库：{len(inserted)}条\n"
                f"重复跳过：{dup}条\n"
                f"相关/Word：{len(digest_articles)}条\n"
                f"国际事件候选：{len(event_candidates)}条\n"
            )
            if filtered_count:
                digest += f"内容过滤：{filtered_count}条\n"
            digest += f"失败来源：{len(failed)}个"
            print(digest)

            if digest_articles:
                word_path = build_word_digest(
                    digest_articles,
                    tmp_reports,
                    generated_at=now,
                    catch_up_urls=classification["catch_up_urls"],
                    importance_results=importance_results,
                    international_config=international_config,
                    international_coverage=intl_coverage,
                    international_translations=international_translations,
                )
                print(f"Dry-run Word：{word_path}")
        finally:
            db.close()
            shutil.rmtree(dry_root, ignore_errors=True)
        return


    # ---- Diagnose Source ---------------------------------------------
    if args.diagnose_source:
        #from .article_identity import article_identity_key
        #from .freshness import filter_fresh_articles
        #from .time_utils import TAIPEI
        print(f"\n=== Diagnose source: {args.diagnose_source} ===\n")
        matched = [s for s in sources if s["id"] == args.diagnose_source]
        if not matched:
            print(f"Source not found: {args.diagnose_source}")
            return
        source = matched[0]
        cls = COLLECTOR_MAP.get(source.get("type", ""))
        if not cls:
            print(f"Unknown collector type: {source.get('type')}")
            return
        if source.get("enabled") is False:
            print(f"  [WARN] Source is disabled (enabled=false)")
        collector = cls(source)
        try:
            articles = collector.collect()
            print(f"  HTTP: 200 (via collector)")
            print(f"  Items in response: {len(articles)}")
            print(f"  Successfully parsed: {len(articles)}")
            aware_count = sum(1 for a in articles if a.published_at and a.published_at.tzinfo)
            naive_count = sum(1 for a in articles if a.published_at and not a.published_at.tzinfo)
            unknown_count = sum(1 for a in articles if not a.published_at)
            now = datetime.now(TAIPEI)
            fr = filter_fresh_articles(articles, now)
            print(f"  Aware times: {aware_count}")
            print(f"  Naive times: {naive_count}")
            print(f"  Unknown times: {unknown_count}")
            print(f"  Fresh: {len(fr.fresh_articles)}")
            if fr.fresh_articles:
                print(f"  Latest: {fr.fresh_articles[0].published_at}")
                print(f"  Oldest: {fr.fresh_articles[-1].published_at}")
            print(f"  URL duplicates: {len(articles) - len(set(a.url for a in articles))}")
            print()
            print("  First 5 articles:")
            for a in articles[:5]:
                print(f"    {a.title[:50]}")
                print(f"    {a.url}")
                print(f"    {a.published_at}")
                print()
        except Exception as e:
            print(f"  ERROR: {e}")
        finally:
            collector.close()
        print("=== Diagnosis complete (read-only, no DB written) ===\n")
        return
    # ---- Export Word ------------------------------------------------
    # ---- Export Word ---------------------------------------------------
    if args.export_word is not None:
        if not db_path.exists():
            print("数据库不存在或为空，请先运行 python -m app.main --bootstrap")
            return
        db = Database(db_path)
        db.connect()
        now = datetime.now(TAIPEI)
        articles = db.get_articles_since(datetime(2000, 1, 1))
        if len(articles) > args.export_word:
            articles = articles[-args.export_word:]
        if not articles:
            print("数据库中暂无新闻。")
            db.close()
            return
        output_dir = project_root / "data" / "reports"
        enrich_summaries_safe(articles, db)
        word_articles, intl_coverage = prepare_international_delivery(
            articles, international_config
        )
        if not word_articles:
            print("没有符合简报范围的新闻。")
            db.close()
            return
        international_translator = _build_international_translator()
        translation_articles = _translation_articles_for_delivery(
            articles, intl_coverage
        )
        international_translations = _precompute_international_translations(
            translation_articles,
            international_config,
            translator=international_translator,
        )
        output_path = build_word_digest(
            word_articles, output_dir, generated_at=now,
            international_config=international_config,
            international_coverage=intl_coverage,
            international_translations=international_translations,
        )
        print(f"Word简报已生成：\n{output_path}")
        logger.info("Word export complete: %s", output_path)
        db.close()
        return

    # ---- Backfill Run -------------------------------------------------
    if args.backfill_run:
        if not db_path.exists():
            print("数据库不存在，请先运行 python -m app.main --bootstrap")
            return
        db = Database(db_path)
        db.connect()

        batch_id = args.backfill_run

        # Check idempotency marker
        marker_dir = project_root / "data" / "backfill_markers"
        marker_path = marker_dir / f"catchup_{batch_id}.json"
        if marker_path.exists():
            # json already imported above
            try:
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
                if marker.get("sent_at") and marker.get("feishu_result") == "success":
                    print(f"批次 {batch_id} 已于 {marker['sent_at']} 成功发送。")
                    print("如需重发，请先删除标记文件：")
                    print(f"  Remove-Item '{marker_path}'")
                    db.close()
                    return
            except Exception:
                pass

        # Get this batch's articles: from a specific time range
        # The batch_id is the run timestamp in format like "20260719_0842"
        # json already imported above
        print(f"=== Backfill run: {batch_id} ===\n")

        now = datetime.now(TAIPEI)
        # Try to parse batch_id as a datetime range marker
        # Format: YYYYMMDD_HHMM (run time)
        batch_dt = None
        try:
            batch_dt = datetime.strptime(batch_id, "%Y%m%d_%H%M")
            batch_dt = batch_dt.replace(tzinfo=TAIPEI)
        except (ValueError, TypeError):
            print(f"批次格式无效: {batch_id}，应为 YYYYMMDD_HHMM")
            db.close()
            return

        # Fetch articles inserted in a 2-hour window around the batch time
        batch_start = batch_dt - timedelta(hours=1)
        batch_end = batch_dt + timedelta(hours=1)
        from datetime import timezone as _tz
        articles = []
        try:
            rows = db.conn.execute(
                "SELECT source_id, source_name, category, title, url, "
                "published_at, fetched_at, position, summary, summary_attempted_at "
                "FROM articles WHERE fetched_at >= ? AND fetched_at <= ? "
                "ORDER BY published_at DESC",
                (batch_start.isoformat(), batch_end.isoformat()),
            ).fetchall()
            from .models import Article as _Article
            articles = [
                _Article(
                    source_id=row[0], source_name=row[1], category=row[2],
                    title=row[3], url=row[4],
                    published_at=datetime.fromisoformat(row[5]) if row[5] else None,
                    fetched_at=datetime.fromisoformat(row[6]),
                    position=row[7],
                    summary=row[8],
                    summary_attempted_at=(
                        datetime.fromisoformat(row[9]) if row[9] else None
                    ),
                )

                for row in rows
            ]
        except Exception as e:
            print(f"查询失败: {e}")
            db.close()
            return

        if not articles:
            print(f"批次 {batch_id} 窗口内没有文章。")
            db.close()
            return

        print(f"Candidate count: {len(articles)}")
        print(f"Will send: {'YES' if args.send else 'NO'}")
        print()
        for i, a in enumerate(articles, 1):
            age = "N/A"
            if a.published_at:
                age_mins = int((now.astimezone(TAIPEI) - a.published_at.astimezone(TAIPEI)).total_seconds() / 60)
                age = f"{age_mins}分钟"
            print(f"{i}. ID={a.url[:40]} / {a.source_name} / {a.published_at} / {age}")
            print(f"   {a.title[:60]}")
            print()

        if not args.send:
            print("=== Dry-run complete (use --send to actually send) ===\n")
            db.close()
            return

        # Actually generate Word and send
        print("=== Generating backfill Word document ===\n")
        try:
            # Mark ALL articles as catch_up for the backfill Word
            all_catch_up = {a.url for a in articles}
            output_dir = project_root / "data" / "reports"
            output_dir.mkdir(parents=True, exist_ok=True)
            enrich_summaries_safe(articles, db)
            word_path = build_word_digest(
                articles, output_dir, generated_at=now,
                catch_up_urls=all_catch_up,
            )
            print(f"Word generated: {word_path}")

            # Send to Feishu
            import os as _os2
            from dotenv import load_dotenv as _ld
            _ld()
            fs_id = _os2.getenv("FEISHU_APP_ID", "").strip()
            fs_secret = _os2.getenv("FEISHU_APP_SECRET", "").strip()
            fs_chat = _os2.getenv("FEISHU_CHAT_ID", "").strip()
            feishu_result = "not_configured"
            if fs_id and fs_secret and fs_chat:
                caption = "【台湾新闻监测｜补发简报】"
                send_document(word_path, fs_id, fs_secret, fs_chat, caption=caption)
                feishu_result = "success"
                print("Feishu: sent successfully")
            else:
                print("Feishu: not configured, skipping send")

            # Write idempotency marker
            marker_dir.mkdir(parents=True, exist_ok=True)
            marker = {
                "batch_id": batch_id,
                "article_count": len(articles),
                "article_ids": [a.url[:60] for a in articles],
                "word_path": str(word_path),
                "sent_at": now.isoformat(),
                "feishu_result": feishu_result,
            }
            marker_path.write_text(json.dumps(marker, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\nIdempotency marker written: {marker_path}")

        except Exception as e:
            print(f"Backfill failed: {e}")
            logger.exception("Backfill run failed: batch_id=%s", batch_id)

        db.close()
        return

        # ---- Default: collect + save + notify with lock -------------------
    lock_path = project_root / "data" / "monitor.lock"
    lock = InstanceLock(lock_path)
    if not lock.acquire():
        msg = "已有另一个采集实例在运行，本次跳过。"
        print(msg)
        logger.warning("Lock acquisition failed, another instance is running")
        return

    db = None
    try:
        db = Database(db_path)
        db.connect()
        db.create_tables()

        # Capture baselines before insertion. A newly enabled source with no
        # history must not deliver old catch-up entries on its first run.
        source_baselines = _get_source_baselines(db, sources)

        inserted, total, dup, failed, run_removed, hist_id_dup, filtered_count = collect_all(
            sources, db, content_filter_config, health_store=health_store
        )
        now = datetime.now(TAIPEI)

        notifier = create_notifier()
        international_translator = _build_international_translator()

        # Load catch-up configuration
        catch_up_enabled = os.getenv("NEWS_CATCHUP_ENABLED", "false").strip().lower() in ("true", "1", "yes", "on")
        try:
            catch_up_max_minutes = int(os.getenv("NEWS_CATCHUP_MAX_MINUTES", "720").strip())
        except (ValueError, AttributeError):
            catch_up_max_minutes = 720

        # Log configuration
        logger.info(
            "Catch-up delivery: %s | Freshness window: %d min | Catch-up window: %d min",
            "enabled" if catch_up_enabled else "disabled",
            90, catch_up_max_minutes,
        )

        # Validate catch-up configuration
        if catch_up_enabled and catch_up_max_minutes <= 90:
            msg = (
                f"NEWS_CATCHUP_MAX_MINUTES ({catch_up_max_minutes}) must be greater than "
                f"NEWS_FRESHNESS_MINUTES (90)"
            )
            logger.error(msg)
            print(msg)
            return

        # Freshness filter with catch-up support
        db_existing = dup - run_removed
        classification = _classify_delivery_articles(
            inserted, source_baselines, now,
            catch_up_enabled=catch_up_enabled,
            catch_up_max_minutes=catch_up_max_minutes,
        )
        fresh_articles = classification['fresh_articles']
        catch_up_eligible = classification['catch_up_eligible']
        catch_up_urls = classification['catch_up_urls']
        stale_articles = classification['stale_articles']
        unknown_articles = classification['unknown_articles']
        future_articles = classification['future_articles']
        baseline_excluded = classification['baseline_excluded']

        delivery_articles = fresh_articles + catch_up_eligible

        # Filter irrelevant international stories and merge duplicate coverage
        # before importance scoring so they cannot consume highlight slots.
        digest_articles, intl_coverage = prepare_international_delivery(
            delivery_articles, international_config
        )
        final_word_count = len(digest_articles)

        # Only deliverable articles need enrichment. The summarizer separately
        # enforces that explicit international access levels never fetch pages.
        enrich_summaries_safe(digest_articles, db)

        # Importance classification
        importance_results = classify_articles(
            digest_articles, importance_rules_config
        )
        pre_cap_summary = importance_summary(importance_results)
        importance_results = finalize_importance(
            importance_results, importance_rules_config
        )
        translation_articles = _translation_articles_for_delivery(
            delivery_articles, intl_coverage
        )
        international_translations = _precompute_international_translations(
            translation_articles,
            international_config,
            translator=international_translator,
        )
        notification_translator = _build_notification_translation_lookup(
            translation_articles, international_translations, intl_coverage
        )
        notification_dedup_store = _notification_dedup_store(
            project_root, international_config, dry_run=False
        )
        logger.info(
            "%s | after cap: %s",
            pre_cap_summary,
            importance_summary(importance_results),
        )

        # International alerts are event-level and deliberately separate from
        # the existing Taiwan notification path.  The candidate builder only
        # sees fresh, relevant clusters at the final importance score; normal
        # international coverage remains Word-only.
        event_candidates = []
        try:
            relevant_delivery, _ = filter_international(
                delivery_articles, international_config or {}
            )
            event_clusters, _ = cluster_international_articles(
                relevant_delivery, international_config or {}
            )
            event_candidates = build_notification_candidates(
                event_clusters,
                importance_results,
                {
                    "fresh_articles": fresh_articles,
                    "catch_up_urls": catch_up_urls,
                    "baseline_excluded": baseline_excluded,
                    "stale_articles": stale_articles,
                    "unknown_articles": unknown_articles,
                    "future_articles": future_articles,
                },
                now,
                translator=notification_translator,
                dedup_store=notification_dedup_store,
            )
            send_succeeded = _deliver_event_candidates(
                notifier, event_candidates, notification_dedup_store, now
            )
            logger.info(
                "International event candidates: %d (sent=%s)",
                len(event_candidates), send_succeeded,
            )
        except Exception as event_err:
            logger.warning("International event notification failed safely: %s", event_err)

        if digest_articles:
            # Ordinary relevant international stories are Word-only.  The
            # existing Taiwan/local digest keeps its historical categories and
            # importance behavior; international event alerts use the
            # event-candidate lane above.
            long_digest_articles = [
                article for article in digest_articles
                if not is_international_media(article.source_name, international_config)
            ]
            if long_digest_articles:
                digest = build_digest(
                    long_digest_articles, now,
                    international_coverage=intl_coverage,
                    international_config=international_config,
                    include_international_media=False,
                )
                stats = (
                    f"\n本轮收集：{total}条"
                )
                if failed:
                    _sep = "、"
                    stats += f"\n失败来源：{len(failed)}个（{_sep.join(failed)}）"
                stats += (
                    f"\nURL去重：{run_removed}条"
                    f"\n历史去重：{db_existing}条"
                    f"\n新增入库：{len(inserted)}条"
                    f"\n内容过滤：{filtered_count}条"
                    f"\n正常新闻：{len(fresh_articles)}条"
                    f"\n补发新闻：{len(catch_up_eligible)}条"
                    f"\n过期跳过：{len(stale_articles)}条"
                    f"\n未知时间：{len(unknown_articles)}条"
                    f"\n未来时间：{len(future_articles)}条"
                    f"\n简报Word：{final_word_count}条"
                )
                digest += stats
                notifier.send_long(digest)
            # Auto-generate Word digest for the international-filtered article set
            try:
                output_dir = project_root / "data" / "reports"
                word_path = build_word_digest(
                    digest_articles, output_dir, generated_at=now,
                    catch_up_urls=catch_up_urls,
                    importance_results=importance_results,
                    international_config=international_config,
                    international_coverage=intl_coverage,
                    international_translations=international_translations,
                )
                logger.info("Word digest saved: %s", word_path)
                # Auto-send to Feishu if credentials are available
                try:
                    import os as _os2
                    from dotenv import load_dotenv as _ld
                    _ld()
                    fs_id = _os2.getenv("FEISHU_APP_ID", "").strip()
                    fs_secret = _os2.getenv("FEISHU_APP_SECRET", "").strip()
                    fs_chat = _os2.getenv("FEISHU_CHAT_ID", "").strip()
                    if fs_id and fs_secret and fs_chat:
                        if os.getenv("DISABLE_FEISHU_SEND", "").strip().lower() not in ("1", "true", "yes"):
                            send_document(
                                word_path, fs_id, fs_secret, fs_chat,
                            )
                            logger.info("Word digest sent to Feishu")

                            # Send highlight card if enabled and has highlights
                            card_cfg = importance_rules_config.get("feishu_highlight_card", {})
                            if card_cfg.get("enabled", True):
                                max_h = importance_rules_config.get("display", {}).get("max_highlights", 10)
                                # 高亮卡片只用进入 digest/Word 的集合（canonical +
                                # 非国际文章），排除不相关国际文章与 coverage 成员
                                digest_urls = {
                                    a.url for a in digest_articles
                                    if not is_international_media(
                                        a.source_name, international_config
                                    )
                                }
                                highlights = select_highlights(
                                    [
                                        (a, r) for a, r in importance_results
                                        if a.url in digest_urls
                                    ],
                                    max_highlights=max_h,
                                )
                                if highlights:
                                    # Send directly via the Feishu App bot so the
                                    # card works even when NOTIFIER=console
                                    # (ConsoleNotifier.send_highlight_card returns
                                    # False by design and would silently skip it).
                                    card = build_highlight_card(
                                        highlights,
                                        title=card_cfg.get("title", "本期重点新闻提示"),
                                        show_summary=card_cfg.get("show_summary", False),
                                        show_source=card_cfg.get("show_source", False),
                                        show_published_at=card_cfg.get("show_published_at", False),
                                    )
                                    send_card(card, fs_id, fs_secret, fs_chat)
                                    logger.info(
                                        "Highlight card sent via Feishu App bot "
                                        "(items=%d, critical=%d)",
                                        len(highlights),
                                        sum(1 for _, r in highlights if r.level == "critical"),
                                    )
                        else:
                            logger.info("Feishu send disabled by DISABLE_FEISHU_SEND")
                except Exception as fs_err:
                    logger.warning("Feishu send failed: %s", fs_err)
            except Exception as e:
                logger.warning("Word digest generation failed: %s", e)
            logger.info(
                "Notified: fresh=%d, catch_up=%d, total=%d",
                len(fresh_articles), len(catch_up_eligible), total,
            )
        else:
            if inserted:
                if delivery_articles and not digest_articles:
                    print(
                        f"\n新增{len(inserted)}条，但国际相关性过滤后无可投递新闻："
                        f"\n  Fresh={len(fresh_articles)}, "
                        f"Catch_up={len(catch_up_eligible)}, "
                        f"Stale={len(stale_articles)}, "
                        f"Unknown={len(unknown_articles)}, Future={len(future_articles)}"
                    )
                elif baseline_excluded and not catch_up_eligible:
                    print(
                        f"\n新增{len(inserted)}条，但无符合交付条件的新闻（详情见日志）："
                        f"\n  Fresh=0, Catch_up_ineligible={len(baseline_excluded)}, "
                        f"Stale={len(stale_articles)}, "
                        f"Unknown={len(unknown_articles)}, Future={len(future_articles)}"
                    )
                else:
                    print(
                        f"\n新增{len(inserted)}条，但无符合交付条件的新闻："
                        f"\n  Fresh=0, Stale={len(stale_articles)}, "
                        f"Unknown={len(unknown_articles)}, Future={len(future_articles)}"
                    )
                logger.info(
                    "No notifiable articles after freshness/relevance: inserted=%d, fresh=%d, catch_up_eligible=%d, stale=%d, unknown=%d, future=%d",
                    len(inserted), len(fresh_articles), len(catch_up_eligible),
                    len(stale_articles), len(unknown_articles), len(future_articles),
                )
            else:
                print("本轮没有新增新闻。")
                logger.info("No new articles inserted")
    except SystemExit:
        raise
    except Exception:
        logger.exception("Unhandled exception")
        raise
    finally:
        if db is not None:
            db.close()
        lock.release()
        logger.info("Taiwan News Monitor ended")



if __name__ == "__main__":
    main()
