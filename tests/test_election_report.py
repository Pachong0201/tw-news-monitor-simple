import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.election_report import build_fact_base, send_existing_report
from app.election_classifier import ElectionClassifier
from app.election_fact_store import ElectionFactStore
from app.election_word_report import build_election_word_report
from docx import Document
import sqlite3
import tempfile
import json
import os

CONFIG_PATH = Path(__file__).resolve().parent.parent / 'config' / 'election_watch.yaml'

class TestFactBase:
    def test_build_facts_for_tainan(self):
        classifier = ElectionClassifier(CONFIG_PATH)
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        conn.execute('''
            CREATE TABLE articles (
                source_id TEXT, source_name TEXT, category TEXT,
                title TEXT, url TEXT PRIMARY KEY,
                published_at TEXT, fetched_at TEXT
            )
        ''')
        conn.execute(
            "INSERT INTO articles VALUES ('cna_politics','中央社','politics',"
            "'林俊憲宣布參選台南市長 民進黨初選啟動','https://cna.tw/1','2026-07-26','2026-07-26')"
        )
        conn.execute(
            "INSERT INTO articles VALUES ('udn_politics','聯合報','politics',"
            "'台南美食節開跑 在地小吃人潮湧現','https://udn.tw/1','2026-07-26','2026-07-26')"
        )
        facts = build_fact_base(None, conn, classifier, 'tainan', days=365)
        assert len(facts) >= 1
        assert any('林俊憲' in f['actor'] for f in facts)
        conn.close()

    def test_build_facts_for_new_taipei(self):
        classifier = ElectionClassifier(CONFIG_PATH)
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        conn.execute('''
            CREATE TABLE articles (
                source_id TEXT, source_name TEXT, category TEXT,
                title TEXT, url TEXT PRIMARY KEY,
                published_at TEXT, fetched_at TEXT
            )
        ''')
        conn.execute(
            "INSERT INTO articles VALUES ('cna_politics','中央社','politics',"
            "'蘇巧慧表態參選新北市長','https://cna.tw/2','2026-07-26','2026-07-26')"
        )
        facts = build_fact_base(None, conn, classifier, 'new_taipei', days=365)
        assert len(facts) >= 1
        assert any('蘇巧慧' in f['actor'] for f in facts)
        conn.close()


def sample_report():
    return {
        "title": "台湾地方选举态势分析",
        "overall_judgment": "总体竞争态势保持开放。",
        "tainan": {"situation": "台南候选人持续整合。", "outlook": "仍需观察提名进程。"},
        "new_taipei": {"situation": "新北布局逐渐清晰。", "outlook": "议题攻防将升温。"},
        "comparison": "两地节奏不同，但组织动员都是关键。",
    }


def test_build_election_word_report_has_required_sections(tmp_path):
    path = build_election_word_report(
        sample_report(),
        {"tainan_facts": [{"source": "中央社"}], "new_taipei_facts": []},
        tmp_path / "report.docx",
        report_date="2026-07-31",
    )
    assert path.exists()
    doc = Document(path)
    text = "\n".join(paragraph.text for paragraph in doc.paragraphs)
    for heading in ["总体判断", "台南选情", "新北选情", "跨区域比较", "证据摘要"]:
        assert heading in text


def test_send_existing_rebuilds_word_and_sends(tmp_path, monkeypatch):
    store = ElectionFactStore(tmp_path / "election.db")
    store.connect()
    store.create_tables()
    json_path = tmp_path / "2026-07-31.json"
    json_path.write_text(
        json.dumps({"_normalized_report": sample_report()}, ensure_ascii=False),
        encoding="utf-8",
    )
    word_path = tmp_path / "report.docx"
    store.conn.execute(
        """INSERT INTO report_runs
           (report_period, cutoff_time, json_path, word_path, generated_at)
           VALUES (?,?,?,?,?)""",
        ("2026-07-31", "2026-07-31", str(json_path), str(word_path), "2026-07-31"),
    )
    store.conn.commit()
    monkeypatch.setenv("FEISHU_APP_ID", "id")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    monkeypatch.setenv("FEISHU_CHAT_ID", "chat")
    monkeypatch.delenv("DISABLE_FEISHU_SEND", raising=False)
    sent = []
    monkeypatch.setattr(
        "app.election_report.send_document",
        lambda path, app_id, app_secret, chat_id: sent.append(Path(path)),
    )

    assert send_existing_report(store, "2026-07-31") is True
    assert word_path.exists()
    assert sent == [word_path]
    row = store.conn.execute(
        "SELECT feishu_status, word_sha256 FROM report_runs WHERE report_period=?",
        ("2026-07-31",),
    ).fetchone()
    assert row[0] == "sent"
    assert len(row[1]) == 64
    store.close()
