# AI Agent Guide

## Overview

`tw-news-monitor-simple` is a minimal Taiwan news monitoring tool.
Collects news from RSS feeds and HTML pages, deduplicates by URL, saves
to SQLite, and sends a categorized digest.

## Key Design Decisions

- Single `Article` dataclass with `slots=True`.
- Single `articles` table in SQLite with UNIQUE constraint on URL.
- URL normalization: lowercase scheme+host+path, remove fragment + trailing slash.
- Each collector has a self-contained `collect()` method returning up to 20 `Article` instances.
- Digest groups by category (politics, economy, international) and sorts by position, published_at (desc), source_name.
- Notifier factory: ConsoleNotifier by default, falls back from Feishu/Telegram gracefully.
- `--dry-run` uses a temporary SQLite DB that is deleted after the run.

## Architecture

```
main.py → load sources → for each source:
  collector.collect() → list[Article]
  db.article_exists(url) → dedup
  db.save_articles(new)
  build_digest(new) → notifier.send(text)
```

## Adding a Source

Add an entry to `config/sources.yaml` with `id`, `name`, `category`,
`collector` type, and `url`. If a new collector type is needed, add a
class in `app/collectors/` that extends `BaseCollector` and implements
`collect()`, then register it in the `COLLECTOR_MAP` in `main.py`.

## Collector Rules

1. Max 20 items per source.
2. Skip empty titles / URLs.
3. Normalize URL before dedup.
4. Do NOT fetch article body.
5. Do NOT filter by keywords inside collectors. Out-of-scope filtering
   (e.g. social trivia in economy feeds) is handled centrally by
   `app/content_filter.py` + `config/content_filter.yaml` before saving.
6. Do NOT classify as breaking news.
7. Single source failure must not stop others.
8. Set timeout per request.
9. Set reasonable User-Agent.
10. Log clear errors on failure.

## What Is NOT Implemented

- Event aggregation
- Title similarity detection
- News scoring / ranking
- Keyword filtering
- Page snapshots
- LLM classification
- Playwright browser automation
- Complex DB architecture (no events/snapshots/reports tables)
- Pydantic / SQLAlchemy / async frameworks / APScheduler

## What This Version Does NOT Have (Previous Project Issues)

The old `tw-news-monitor` had severe indentation and file-overwriting
problems. This project is rebuilt from scratch without copying any
Python code from the old project.

## 台南选情研判 V1 生产契约（2026-08-15 定型，research-driven）

- 生产路径：`app/assessment/research_driven/`（`assessment_mode=research_driven`）。
  调度入口 `python -m app.assessment.research_driven.scheduled` →
  `generation.run_generation()`。流程：Period Gate（facts_cutoff >= period_end，
  否则 REPORT_PERIOD_NOT_READY）→ Research Pack（只读正式事实）→ 单次 LLM 生成
  （analysis_plan 审计 JSON + final_article 文章）→ Fact Safety Check → Word →
  ready_for_review。最高原则：事实层严格，分析层开放。
- 产出目录：`data/election_assessment/tainan_2026/production/periods/YYYYMMDD_YYYYMMDD/`
  （research_pack.json / ASSESSMENT_RESEARCH_PACK.md / analysis_plan.json /
  final_article.md / final_article.docx / fact_safety_audit.json /
  review_notes.json / run_metadata.json / input_manifest.json），
  最新期预览复制到 production 根（FINAL_ASSESSMENT_PREVIEW.md/.docx）。
- 研究包即模型唯一事实基础；Markdown 版可单独上传 ChatGPT 人工生成（Fallback）。
  事实层（人物/日期/事件/数字/民调/来源）必须来自研究包；分析层（变化识别、
  核心判断、因果链、权力关系、政治意图、趋势推演）开放给模型。
- 门禁只有 Fact Safety Check：严重问题（未来事件泄漏、虚构民调数字等）才
  HARD_BLOCK；轻微问题一律 review_note，不阻止成文。
- LLM 适配：`research_driven/adapter.py`（provider/model/temperature/max_tokens
  配置化，配置在 `config/election_assessment.yaml` 的 `llm.research_driven`）。
- 旧 Claim-centric 路径（`generate_llm_report` / `claim_*` / `r2/generation.py` /
  `two_stage_*`）保留为 legacy：不删除、不继续开发、不进入生产（
  `config/election_assessment.yaml` 中 `legacy_assessment_generation_modes`）。
- 幂等：同周期已生成不重复调用模型；`--force-regenerate` 显式重跑（先快照旧记录）。
- 调度：复用现有计划任务 `Tainan Election Assessment`（每月 9/22 日 09:00
  Asia/Taipei），只生成到人工终审，绝不自动发送飞书（DISABLE_FEISHU_SEND=1）。
- 人工终审：`python -m app.assessment.research_driven.review list/show/approve/reject`；
  操作手册见 `ASSESSMENT_OPERATOR_GUIDE.md`。

## 项目主文件夹（2026-08-11 整合后）

- 唯一主文件夹/生产目录：`D:\WXWorkLocal\TW News-Monitor111\tw-news-monitor-simple`
- 所有计划任务（Taiwan News Monitor、Tainan Election Candidate Monitor、
  Tainan Election Assessment）均指向本目录；旧事件管道任务
  `Taiwan News Event Pipeline` 已停用（被候选管线取代）。
- 历史副本（`D:\0801tw-news-monitor-simple`、`tw-news-monitor-simple-0809`、
  `-dev`、`-election-dev`、`.BACKUP`、`D:\TW News-Monitor`、`D:\news monitor`）
  已停止同步，仅供归档参考，不要再作为改动目标。
- 后续所有代码与配置改动只在本主文件夹进行；`data/news.db` 与
  `data/election_watch.db` 是实时运行数据（会随计划任务增长），
  相关测试保护的是完整性而非字节哈希。
