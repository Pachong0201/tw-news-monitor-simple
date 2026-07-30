import argparse
import logging
import sys
import os
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
import json

import yaml

from .collectors import RSSCollector, UDNCollector, EBCCollector, CNAHtmlCollector, LtnRSSCollector, PresidentCollector
from .database import Database
from .digest import build_digest
from .lock import InstanceLock
from .notifier import create_notifier
from .feishu import send_document
from .word_digest import build_word_digest
from .freshness import FreshnessResult, filter_fresh_articles
from .source_registry import is_official_source, get_source_info, get_official_sources
from .time_utils import TAIPEI
from .importance import classify_articles, importance_summary, load_rules, select_highlights
from .article_identity import article_identity_key, deduplicate_articles_by_identity

logger = logging.getLogger(__name__)

COLLECTOR_MAP = {
    "rss": RSSCollector,
    "udn": UDNCollector,
    "ebc": EBCCollector,
    "cna_list_html": CNAHtmlCollector,
    "ltn_rss": LtnRSSCollector,
    "newtalk_rss": RSSCollector,
    "president_json": PresidentCollector,
}


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
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)["sources"]


def validate_sources_config(sources: list[dict], collector_map: dict) -> None:
    """Validate source config before any network/DB/Word/Feishu operations.

    Rules: id non-empty + unique, type non-empty + in map, enabled is real bool,
    url non-empty, category valid, collector-only without type errors.
    Exits with code 1 on any error.
    """
    VALID_CATEGORIES = {"politics", "economy", "international"}
    errors = []
    seen_ids = set()

    for i, source in enumerate(sources):
        sid = source.get("id", f"<index {i}>")

        if not source.get("id"):
            errors.append(f"[<index {i}>] id is empty or missing")
            continue

        if sid in seen_ids:
            errors.append(f"[{sid}] duplicate source id")
        seen_ids.add(sid)

        stype = source.get("type")
        if not stype:
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

        cat = source.get("category")
        if cat and cat not in VALID_CATEGORIES:
            allowed = ", ".join(sorted(VALID_CATEGORIES))
            errors.append(f"[{sid}] category='{cat}' is not valid (allowed: {allowed})")

        if "enabled" in source:
            enabled_val = source["enabled"]
            if not isinstance(enabled_val, bool):
                errors.append(
                    f"[{sid}] enabled={repr(enabled_val)} must be a boolean "
                    f"(True/False), not string"
                )

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


def collect_all(
    sources: list[dict], db: Database,
) -> tuple[list, int, int, list[str]]:
    """Collect news, dedup by URL, save new ones.

    Returns (all_inserted, total_fetched, dup_count, failed_sources).
    """
    all_raw: list = []
    total_fetched = 0
    failed: list[str] = []

    for source in sources:
        cls = COLLECTOR_MAP.get(source.get("type"))
        if not cls:
            logger.warning("Unknown collector: %s", source.get("type"))
            continue
        if source.get("enabled") is False:
            continue
        collector = cls(source)
        try:
            articles = collector.collect()
            total_fetched += len(articles)
            logger.info("collected %s: fetched=%d", source["id"], len(articles))
            all_raw.extend(articles)
            print(f"  [OK] {source['id']}: fetched={len(articles)}")
        except Exception as e:
            failed.append(source["id"])
            logger.error("Failed %s: %s", source["id"], e)
            print(f"  [ERR] {source['id']}: {e}")
        finally:
            collector.close()

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
    inserted = db.save_articles(candidates)

    run_removed = len(run_dups) + len(identity_run_dups)
    dup_count = total_fetched - len(inserted)
    logger.info(
        "Total: fetched=%d, run_url_removed=%d, run_id_removed=%d, "
        "hist_url_dup=%d, hist_id_dup=%d, inserted=%d, failed=%d",
        total_fetched, len(run_dups), len(identity_run_dups),
        len(hist_url_dups), len(hist_id_dups),
        len(inserted), len(failed),
    )
    return inserted, total_fetched, dup_count, failed, run_removed, len(hist_id_dups)


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
    db_path_str = os.getenv("NEWS_DB_PATH", "")
    if db_path_str:
        db_path = Path(db_path_str)
    else:
        db_path = project_root / "data" / "news.db"

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
        inserted, total, dup, failed, run_removed, hist_id_dup = collect_all(sources, db)
        print()
        print("初始化完成：")
        print(f"  本轮采集：{total}条")
        print(f"  新增入库：{len(inserted)}条")
        print(f"  重复跳过：{dup}条")
        if failed:
            print(f"\n失败来源：{len(failed)}个（{', '.join(failed)}）")
        else:
            print("  失败来源：0个")
        logger.info(
            "Bootstrap done: total=%d, new=%d, dup=%d, failed=%d",
            total, len(inserted), dup, len(failed),
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
        tmp_path = project_root / "data" / "dry_run.db"
        db = Database(tmp_path)
        db.connect()
        db.create_tables()
        inserted, total, dup, failed, run_removed, hist_id_dup = collect_all(sources, db)
        now = datetime.now(TAIPEI)
        print()
        if inserted:
            digest = build_digest(inserted, now)
            digest += (
                f"\n本轮收集：{total}条\n"
                f"新增入库：{len(inserted)}条\n"
                f"重复跳过：{dup}条\n"
            )
            if failed:
                digest += f"失败来源：{len(failed)}个（{', '.join(failed)}）\n"
            else:
                digest += "失败来源：0个\n"
            print(digest)
        else:
            digest = build_digest([], now)
            digest += f"\n收集来源：{len(sources)}个\n"
            if failed:
                digest += f"失败来源：{len(failed)}个（{', '.join(failed)}）\n"
            print(digest)
        db.close()
        if tmp_path.exists():
            tmp_path.unlink()
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
        output_path = build_word_digest(articles, output_dir, generated_at=now)
        print(f"Word简报已生成：\n{output_path}")
        logger.info("Word export complete: %s", output_path)
        db.close()
        return

    # ---- Backfill Run -------------------------------------------------
    if args.backfill_run:
        if not db_path.exists():
            print("??????????? python -m app.main --bootstrap")
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
                    print(f"?? {batch_id} ??????????{marker['sent_at']}")
                    print("???????????????????")
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
            print(f"????????: {batch_id}?????? YYYYMMDD_HHMM")
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
                "published_at, fetched_at, position "
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
                )
                for row in rows
            ]
        except Exception as e:
            print(f"???????: {e}")
            db.close()
            return

        if not articles:
            print(f"??? {batch_id} ??????????")
            db.close()
            return

        print(f"Candidate count: {len(articles)}")
        print(f"Will send: {'YES' if args.send else 'NO'}")
        print()
        for i, a in enumerate(articles, 1):
            age = "N/A"
            if a.published_at:
                age_mins = int((now.astimezone(TAIPEI) - a.published_at.astimezone(TAIPEI)).total_seconds() / 60)
                age = f"{age_mins}??"
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
                caption = "????????????"
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
        msg = "??????????????????"
        print(msg)
        logger.warning("Lock acquisition failed, another instance is running")
        return

    db = None
    try:
        db = Database(db_path)
        db.connect()
        db.create_tables()

        inserted, total, dup, failed, run_removed, hist_id_dup = collect_all(sources, db)
        now = datetime.now(TAIPEI)

        notifier = create_notifier()

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

        # Get source baselines (count of existing articles per source BEFORE this run)
        source_baselines: dict[str, int] = {}
        for s in sources:
            sid = s["id"]
            try:
                cnt = db.conn.execute(
                    "SELECT COUNT(*) FROM articles WHERE source_id = ?", (sid,)
                ).fetchone()[0]
                source_baselines[sid] = cnt
            except Exception:
                source_baselines[sid] = 0

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
        final_word_count = len(delivery_articles)
        # Importance classification
        importance_results = classify_articles(
            delivery_articles, importance_rules_config
        )
        logger.info(importance_summary(importance_results))

        if fresh_articles or catch_up_eligible:
            digest = build_digest(fresh_articles + catch_up_eligible, now)
            stats = (
                f"\n???????{total}?"
            )
            if failed:
                _sep = "?"
                stats += f"\n???????{len(failed)}??{_sep.join(failed)}?"
            stats += (
                f"\n??URL?????{run_removed}?"
                f"\n????????{db_existing}?"
                f"\n???????{len(inserted)}?"
                f"\n??????{len(fresh_articles)}?"
                f"\n???????{len(catch_up_eligible)}?"
                f"\n?????{len(stale_articles)}?"
                f"\n???????{len(unknown_articles)}?"
                f"\n???????{len(future_articles)}?"
                f"\n????Word?{final_word_count}?"
            )
            digest += stats
            notifier.send_long(digest)
            # Auto-generate Word digest for delivery articles
            try:
                output_dir = project_root / "data" / "reports"
                word_path = build_word_digest(
                    delivery_articles, output_dir, generated_at=now,
                    catch_up_urls=catch_up_urls,
                    importance_results=importance_results,
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
                                highlights = select_highlights(
                                    importance_results, max_highlights=max_h,
                                )
                                if highlights:
                                    sent = notifier.send_highlight_card(highlights)
                                    if sent:
                                        logger.info("Highlight card sent (items=%d, critical=%d)",
                                                    len(highlights),
                                                    sum(1 for _, r in highlights if r.level == "critical"))
                                    else:
                                        logger.warning("Highlight card send attempted but failed")
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
                if baseline_excluded and not catch_up_eligible:
                    print(
                        f"\n??????{len(inserted)}?????????????????????????"
                        f"\n  Fresh=0, Catch_up_ineligible={len(baseline_excluded)}, "
                        f"Stale={len(stale_articles)}, "
                        f"Unknown={len(unknown_articles)}, Future={len(future_articles)}"
                    )
                else:
                    print(
                        f"\n??????{len(inserted)}?????????????????????"
                        f"\n  Fresh=0, Stale={len(stale_articles)}, "
                        f"Unknown={len(unknown_articles)}, Future={len(future_articles)}"
                    )
                logger.info(
                    "No notifiable articles: inserted=%d, fresh=%d, catch_up_eligible=%d, stale=%d, unknown=%d, future=%d",
                    len(inserted), len(fresh_articles), len(catch_up_eligible),
                    len(stale_articles), len(unknown_articles), len(future_articles),
                )
            else:
                print("?????????")
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
