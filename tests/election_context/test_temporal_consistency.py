import sqlite3

from app.election_context.repository import (
    CREATE_SNAPSHOTS, ElectionContextRepository,
)
from app.election_context.retriever import build_election_context


ELECTION_ID = "TW-TEST"


def make_repo(tmp_path):
    repo = ElectionContextRepository(tmp_path / "context.db")
    repo.connect()
    repo.create_tables()
    return repo


def save_event(repo, event_id, occurred_at, *, status="verified", event_type="news"):
    repo.save_event({
        "event_id": event_id,
        "election_id": ELECTION_ID,
        "occurred_at": occurred_at,
        "event_type": event_type,
        "title": event_id,
        "fact_status": status,
        "significance_score": 80,
    })


def snapshot(snapshot_id, as_of, status="active"):
    return {
        "snapshot_id": snapshot_id,
        "election_id": ELECTION_ID,
        "as_of": as_of,
        "state_json": {"name": snapshot_id},
        "supporting_event_ids": [],
        "created_at": as_of,
        "snapshot_status": status,
    }


def test_search_excludes_superseded_unless_explicit(tmp_path):
    repo = make_repo(tmp_path)
    save_event(repo, "current", "2026-07-01T00:00:00+08:00")
    save_event(
        repo, "old", "2026-07-02T00:00:00+08:00", status="superseded",
    )

    assert [event["event_id"] for event in repo.search_events(
        election_id=ELECTION_ID,
    )] == ["current"]
    assert [event["event_id"] for event in repo.search_events(
        election_id=ELECTION_ID, fact_status="superseded",
    )] == ["old"]
    assert {event["event_id"] for event in repo.search_events(
        election_id=ELECTION_ID, include_superseded=True,
    )} == {"current", "old"}
    repo.close()


def test_as_of_is_inclusive_upper_bound_everywhere(tmp_path):
    repo = make_repo(tmp_path)
    save_event(repo, "before", "2026-07-20T10:00:00+08:00")
    save_event(repo, "at", "2026-07-31T12:00:00+08:00", event_type="party_nomination")
    save_event(repo, "future", "2026-08-01T00:00:00+08:00", event_type="party_nomination")

    context = build_election_context(
        repo, ELECTION_ID, as_of="2026-07-31T12:00:00+08:00",
        recent_days=30,
    )
    recent_ids = {event["event_id"] for event in context["recent_events"]}
    milestone_ids = {event["event_id"] for event in context["milestones"]}
    assert "before" in recent_ids
    assert "at" in recent_ids
    assert "future" not in recent_ids
    assert "at" in milestone_ids
    assert "future" not in milestone_ids
    repo.close()


def test_active_snapshot_is_unique_and_historical_lookup_works(tmp_path):
    repo = make_repo(tmp_path)
    repo.save_snapshot(snapshot("old", "2026-06-01T00:00:00+08:00"))
    repo.save_snapshot(snapshot("new", "2026-07-01T00:00:00+08:00"))

    statuses = dict(repo.conn.execute(
        "SELECT snapshot_id, snapshot_status FROM election_state_snapshots"
    ).fetchall())
    assert statuses == {"old": "superseded", "new": "active"}
    assert repo.get_latest_snapshot(ELECTION_ID)["snapshot_id"] == "new"
    assert repo.get_latest_snapshot(
        ELECTION_ID, as_of="2026-06-15T00:00:00+08:00",
    )["snapshot_id"] == "old"
    repo.close()


def test_create_tables_migrates_duplicate_active_snapshots(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute(CREATE_SNAPSHOTS)
    for item in [
        snapshot("older", "2026-06-01T00:00:00+08:00"),
        snapshot("newer", "2026-07-01T00:00:00+08:00"),
    ]:
        conn.execute(
            """INSERT INTO election_state_snapshots
               (snapshot_id,election_id,as_of,state_json,created_at,snapshot_status)
               VALUES (?,?,?,?,?,?)""",
            (item["snapshot_id"], ELECTION_ID, item["as_of"], "{}",
             item["created_at"], "active"),
        )
    conn.commit()
    conn.close()

    repo = ElectionContextRepository(db_path)
    repo.connect()
    repo.create_tables()
    rows = repo.conn.execute(
        """SELECT snapshot_id, snapshot_status FROM election_state_snapshots
           ORDER BY snapshot_id"""
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("newer", "active"), ("older", "superseded"),
    ]
    repo.close()
