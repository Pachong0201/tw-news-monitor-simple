import argparse
import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv

from app.election_fact_store import ElectionFactStore
from app.election_classifier import ElectionClassifier
from app.election_quality_check import ElectionQualityCheck
from app.election_utils import format_taipei_date
from app.deepseek_analysis import DeepSeekClient
from app.election_word_report import build_election_word_report
from app.feishu import send_document

TAIPEI = timezone(timedelta(hours=8))
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / 'config' / 'election_watch.yaml'
STYLE_PATH = PROJECT_ROOT / 'config' / 'election_analysis_style.yaml'
DB_PATH = PROJECT_ROOT / 'data' / 'election_watch.db'
NEWS_DB_PATH = PROJECT_ROOT / 'data' / 'news.db'

import yaml

def inject_manual_facts(facts_list, city_key):
    mpath = PROJECT_ROOT / 'config' / 'election_manual_facts.json'
    if not mpath.exists():
        return
    try:
        with open(mpath, encoding='utf-8') as _f:
            manual = json.load(_f)
        items = manual.get(city_key, [])
        existing_urls = {f.get('url', '') for f in facts_list}
        added = 0
        for mf in items:
            if mf.get('url', 'manual') not in existing_urls:
                mf['fact_id'] = f"manual_{city_key}_{len(facts_list)+added+1}"
                facts_list.append(mf)
                added += 1
        if added:
            print(f'人工事实 {city_key}: +{added}')
    except Exception as _e:
        print(f'人工事实加载 {city_key}: {_e}')

def load_style() -> dict:
    with open(STYLE_PATH, encoding='utf-8') as f:
        return yaml.safe_load(f)


def _feishu_send_disabled() -> bool:
    return os.getenv('DISABLE_FEISHU_SEND', '').strip().lower() in (
        '1', 'true', 'yes',
    )


def send_existing_report(
    store: ElectionFactStore,
    report_period: str,
    *,
    no_send: bool = False,
) -> bool:
    """Verify/rebuild an existing Word report and actually send it."""
    row = store.conn.execute(
        """SELECT word_path, json_path, feishu_status
           FROM report_runs WHERE report_period=?""",
        (report_period,),
    ).fetchone()
    if not row:
        print(f'未找到报告 {report_period}')
        return False

    word_path_raw, json_path_raw, _status = row
    if not json_path_raw and word_path_raw and str(word_path_raw).endswith('.json'):
        json_path_raw = word_path_raw
        word_path_raw = ''
    json_path = Path(json_path_raw) if json_path_raw else (
        PROJECT_ROOT / 'data' / 'reports' / 'election' / f'{report_period}.json'
    )
    if not json_path.exists():
        raise FileNotFoundError(f'报告 JSON 不存在：{json_path}')
    report_payload = json.loads(json_path.read_text(encoding='utf-8'))
    report = report_payload.get('_normalized_report', report_payload)
    evidence_path = (
        PROJECT_ROOT / 'data' / 'reports' / 'election' / 'evidence'
        / f'{report_period}.evidence.json'
    )
    evidence = (
        json.loads(evidence_path.read_text(encoding='utf-8'))
        if evidence_path.exists() else {}
    )
    word_path = Path(word_path_raw) if word_path_raw else (
        PROJECT_ROOT / 'data' / 'reports' / 'election'
        / f'台湾选举态势分析_{report_period}.docx'
    )
    if not word_path.exists() or word_path.suffix.lower() != '.docx':
        word_path = build_election_word_report(
            report, evidence, word_path.with_suffix('.docx'),
            report_date=report_period,
        )
    word_sha256 = hashlib.sha256(word_path.read_bytes()).hexdigest()
    store.conn.execute(
        """UPDATE report_runs SET word_path=?, json_path=?, word_sha256=?
           WHERE report_period=?""",
        (str(word_path), str(json_path), word_sha256, report_period),
    )
    store.conn.commit()

    if no_send or _feishu_send_disabled():
        print(f'报告已就绪但未发送：{word_path}')
        return True
    app_id = os.getenv('FEISHU_APP_ID', '').strip()
    app_secret = os.getenv('FEISHU_APP_SECRET', '').strip()
    chat_id = os.getenv('FEISHU_CHAT_ID', '').strip()
    if not app_id or not app_secret or not chat_id:
        store.conn.execute(
            "UPDATE report_runs SET feishu_status='pending_credentials' WHERE report_period=?",
            (report_period,),
        )
        store.conn.commit()
        raise RuntimeError('飞书应用凭证未配置完整')
    try:
        send_document(word_path, app_id, app_secret, chat_id)
    except Exception:
        store.conn.execute(
            "UPDATE report_runs SET feishu_status='failed' WHERE report_period=?",
            (report_period,),
        )
        store.conn.commit()
        raise
    store.conn.execute(
        "UPDATE report_runs SET feishu_status='sent' WHERE report_period=?",
        (report_period,),
    )
    store.conn.commit()
    print(f'报告已发送：{word_path}')
    return True

def build_fact_base(store: ElectionFactStore, news_conn, classifier: ElectionClassifier,
                    city: str, days: int = 30) -> list[dict]:
    cutoff = (datetime.now(TAIPEI) - timedelta(days=days)).isoformat()
    rows = news_conn.execute(
        'SELECT source_id, source_name, category, title, url, published_at, fetched_at '
        'FROM articles WHERE fetched_at >= ? ORDER BY fetched_at', (cutoff,)
    ).fetchall()
    facts = []
    for row in rows:
        article = dict(row)
        results = classifier.classify_article(article['title'], article['category'], article['source_name'])
        for r in results:
            if r['city'] != city:
                continue
            facts.append({
                'fact_id': f"fact_{city}_{len(facts)+1}",
                'city': city,
                'date': article.get('published_at', ''),
                'actor': ','.join(r.get('matched_people', [])),
                'action': article['title'],
                'party': ','.join(r.get('matched_parties', [])),
                'issue': ','.join(r.get('matched_issues', [])),
                'election_significance': r['relevance'],
                'source': article['source_name'],
                'title': article['title'],
                'url': article['url'],
                'multi_source': 'no',
                'confidence': r['relevance'],
                'is_poll': '民調' in article['title'] or '民调' in article['title'],
            })
    return facts

def main():
    parser = argparse.ArgumentParser(description='Election Report Generator')
    parser.add_argument('--date', type=str, default=format_taipei_date())
    parser.add_argument('--facts-only', action='store_true')
    parser.add_argument('--no-send', action='store_true')
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--send-existing', action='store_true')
    parser.add_argument('--db', type=str, default=str(NEWS_DB_PATH))
    args = parser.parse_args()
    load_dotenv()
    with open(CONFIG_PATH, encoding='utf-8') as f:
        watch_config = yaml.safe_load(f)
    style_config = load_style()
    classifier = ElectionClassifier(CONFIG_PATH)
    store = ElectionFactStore(DB_PATH)
    store.connect()
    store.create_tables()

    analysis_mode = os.getenv('ANALYSIS_MODE', 'deepseek').strip().lower()
    report_date = args.date
    report_period = f"{report_date}"

    if args.send_existing:
        print('send-existing: 校验并发送已生成的报告')
        try:
            send_existing_report(store, report_period, no_send=args.no_send)
        finally:
            store.close()
        return

    if store.is_report_generated(report_period) and not args.force:
        print(f'报告 {report_period} 已生成，跳过（使用 --force 强制重新生成）')
        store.close()
        return

    import sqlite3
    news_conn = sqlite3.connect(str(args.db))
    news_conn.row_factory = sqlite3.Row

    if args.facts_only or analysis_mode == 'facts_only':
        tainan_facts = build_fact_base(store, news_conn, classifier, 'tainan', days=200)
        nt_facts = build_fact_base(store, news_conn, classifier, 'new_taipei', days=200)
        inject_manual_facts(tainan_facts, 'tainan')
        inject_manual_facts(nt_facts, 'new_taipei')
        facts_dir = PROJECT_ROOT / 'data' / 'reports' / 'election' / 'facts'
        facts_dir.mkdir(parents=True, exist_ok=True)
        tainan_path = facts_dir / f'台南事实底表_{report_date}.json'
        nt_path = facts_dir / f'新北事实底表_{report_date}.json'
        with open(tainan_path, 'w', encoding='utf-8') as f:
            json.dump(tainan_facts, f, ensure_ascii=False, indent=2)
        with open(nt_path, 'w', encoding='utf-8') as f:
            json.dump(nt_facts, f, ensure_ascii=False, indent=2)
        print(f'facts_only模式: 已生成事实底表')
        print(f'  台南: {len(tainan_facts)} 条事实 -> {tainan_path}')
        print(f'  新北: {len(nt_facts)} 条事实 -> {nt_path}')
        news_conn.close()
        store.close()
        return

    api_key = os.getenv('DEEPSEEK_API_KEY', '')
    if not api_key:
        print('未配置 DEEPSEEK_API_KEY')
        news_conn.close()
        store.close()
        return

    client = DeepSeekClient(
        api_key=api_key,
        base_url=os.getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.com'),
        model=os.getenv('DEEPSEEK_MODEL', 'deepseek-chat'),
        timeout=int(os.getenv('DEEPSEEK_TIMEOUT_SECONDS', '180')),
        max_retries=int(os.getenv('DEEPSEEK_MAX_RETRIES', '2')),
    )

    tainan_facts = build_fact_base(store, news_conn, classifier, 'tainan', days=200)
    nt_facts = build_fact_base(store, news_conn, classifier, 'new_taipei', days=200)

    inject_manual_facts(tainan_facts, 'tainan')
    inject_manual_facts(nt_facts, 'new_taipei')

    with open(PROJECT_ROOT / 'prompts' / 'election_analysis_system.md', encoding='utf-8') as f:
        system_prompt = f.read()
    with open(PROJECT_ROOT / 'prompts' / 'election_analysis_final.md', encoding='utf-8') as f:
        final_prompt = f.read()

    tainan_compact = [{'date':f.get('date',''),'actor':f.get('actor',''),'action':f.get('action',''),'issue':f.get('issue',''),'significance':f.get('election_significance','')} for f in tainan_facts]
    nt_compact = [{'date':f.get('date',''),'actor':f.get('actor',''),'action':f.get('action',''),'issue':f.get('issue',''),'significance':f.get('election_significance','')} for f in nt_facts]
    user_message = (
        f"报告日期：{report_date}\n\n"
        f"以下事实底表中的每一条都必须被纳入分析考量。\n\n"
        f"台南事实底表（{len(tainan_compact)}条）：\n{json.dumps(tainan_compact, ensure_ascii=False, indent=2, default=str)[:8000]}\n\n"
        f"新北事实底表（{len(nt_compact)}条）：\n{json.dumps(nt_compact, ensure_ascii=False, indent=2, default=str)[:8000]}"
    )
    final_result = client.analyze(f'{system_prompt}\n\n{final_prompt}', user_message)
    if final_result.get('status') != 'success':
        print(f'DeepSeek报告生成失败: {final_result.get("error")}')
        news_conn.close()
        store.close()
        return

    def _get_str(d, *keys):
        for k in keys:
            v = d.get(k, '')
            if isinstance(v, dict):
                v = v.get('situation', '') or v.get('content', '')
            if isinstance(v, str) and v.strip():
                return v
        return ''

    overall = _get_str(final_result, 'overall_judgment', 'overview', 'section_1')
    tainan_text = _get_str(final_result, 'tainan', 'section_2')
    nt_text = _get_str(final_result, 'new_taipei', 'section_3')
    comparison = _get_str(final_result, 'comparison', 'synthesis', 'section_4')
    sections = final_result.get('sections', [])
    for s in sections:
        if isinstance(s, str):
            continue
        sec = s.get('section') or s.get('title', '')
        content = s.get('content', '')
        if not content:
            continue
        if not overall and any(k in sec for k in ['总体格局', '一、']):
            overall = content
        if not tainan_text and any(k in sec for k in ['台南', '二、']):
            tainan_text = content
        if not nt_text and any(k in sec for k in ['新北', '三、']):
            nt_text = content
        if not comparison and any(k in sec for k in ['综合判断', '四、']):
            comparison = content
    normalized = {
        'title': _get_str(final_result, 'title', 'report_title', ''),
        'overall_judgment': overall,
        'tainan': {'situation': tainan_text, 'outlook': ''},
        'new_taipei': {'situation': nt_text, 'outlook': ''},
        'comparison': comparison,
    }

    qc = ElectionQualityCheck(style_config)
    errors = qc.check_report(normalized, len(tainan_facts) + len(nt_facts))
    print(qc.summary(errors))
    for e in errors:
        if e['status'] == 'fail':
            print(f"  FAIL: {e['check']} - {e.get('detail', '')}")

    if not qc.all_pass(errors) and not args.force:
        print('质检未通过，保存草稿，不发送')
        draft_dir = PROJECT_ROOT / 'data' / 'reports' / 'election' / 'drafts'
        draft_dir.mkdir(parents=True, exist_ok=True)
        draft_path = draft_dir / f'draft_{report_date}.json'
        with open(draft_path, 'w', encoding='utf-8') as f:
            json.dump(final_result, f, ensure_ascii=False, indent=2)
        print(f'草稿保存至: {draft_path}')
        news_conn.close()
        store.close()
        return

    evidence_dir = PROJECT_ROOT / 'data' / 'reports' / 'election' / 'evidence'
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = evidence_dir / f'{report_date}.evidence.json'
    evidence = {
        'report_date': report_date,
        'tainan_facts': tainan_facts,
        'new_taipei_facts': nt_facts,
        'quality_check': errors,
    }
    with open(evidence_path, 'w', encoding='utf-8') as f:
        json.dump(evidence, f, ensure_ascii=False, indent=2)

    report_dir = PROJECT_ROOT / 'data' / 'reports' / 'election'
    report_dir.mkdir(parents=True, exist_ok=True)
    report_json_path = report_dir / f'{report_date}.json'
    report_payload = dict(final_result)
    report_payload['_normalized_report'] = normalized
    with open(report_json_path, 'w', encoding='utf-8') as f:
        json.dump(report_payload, f, ensure_ascii=False, indent=2)

    word_path = report_dir / f'台湾选举态势分析_{report_date}.docx'
    build_election_word_report(
        normalized, evidence, word_path, report_date=report_date,
    )
    word_sha256 = hashlib.sha256(word_path.read_bytes()).hexdigest()

    total_tokens = (final_result.get('input_tokens', 0) + final_result.get('output_tokens', 0))

    feishu_status = 'not_sent'
    if not args.no_send and not _feishu_send_disabled():
        app_id = os.getenv('FEISHU_APP_ID', '').strip()
        app_secret = os.getenv('FEISHU_APP_SECRET', '').strip()
        chat_id = os.getenv('FEISHU_CHAT_ID', '').strip()
        if app_id and app_secret and chat_id:
            try:
                send_document(word_path, app_id, app_secret, chat_id)
                feishu_status = 'sent'
            except Exception as exc:
                feishu_status = 'failed'
                logger.exception('飞书发送选举报告失败: %s', exc)
        else:
            feishu_status = 'pending_credentials'

    store.conn.execute('''
        INSERT OR REPLACE INTO report_runs
        (report_period, cutoff_time, fact_count, event_count,
         deepseek_model, api_status, input_tokens, output_tokens,
         word_path, json_path, word_sha256, feishu_status, generated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        report_period, datetime.now(TAIPEI).isoformat(),
        len(tainan_facts) + len(nt_facts),
        store.get_event_count(),
        os.getenv('DEEPSEEK_MODEL', 'deepseek-chat'),
        'success', total_tokens // 2, total_tokens // 2,
        str(word_path), str(report_json_path), word_sha256, feishu_status,
        datetime.now(TAIPEI).isoformat(),
    ))
    store.conn.commit()
    print(f'报告 JSON 已生成: {report_json_path}')
    print(f'报告 Word 已生成: {word_path}')
    print(f'飞书状态: {feishu_status}')

    store.close()
    news_conn.close()

if __name__ == '__main__':
    main()
