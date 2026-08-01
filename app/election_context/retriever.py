from datetime import datetime, timedelta

from ..time_utils import TAIPEI
from .repository import ElectionContextRepository


def _as_of_window(as_of: str | None, recent_days: int) -> tuple[str, str]:
    """Return an inclusive recent-event window in Taipei time."""
    if as_of:
        value = as_of.strip()
        if len(value) == 10:
            upper = datetime.fromisoformat(value).replace(
                hour=23, minute=59, second=59, microsecond=999999,
                tzinfo=TAIPEI,
            )
        else:
            upper = datetime.fromisoformat(value)
            if upper.tzinfo is None:
                upper = upper.replace(tzinfo=TAIPEI)
            else:
                upper = upper.astimezone(TAIPEI)
    else:
        upper = datetime.now(TAIPEI)
    lower = upper - timedelta(days=recent_days)
    return lower.isoformat(), upper.isoformat()


def build_election_context(
    repo: ElectionContextRepository,
    election_id: str,
    query_text: str = '',
    actors: list[str] | None = None,
    issues: list[str] | None = None,
    as_of: str | None = None,
    recent_days: int = 90,
    max_events: int = 20,
) -> dict:
    date_from, date_to = _as_of_window(as_of, recent_days)

    snapshot = repo.get_latest_snapshot(
        election_id, as_of=date_to if as_of else None,
    )
    recent = repo.search_events(
        election_id=election_id,
        date_from=date_from,
        date_to=date_to,
        min_significance=30,
        limit=max_events,
    )
    historical = repo.search_events(
        election_id=election_id,
        keyword=query_text,
        actors=actors,
        issues=issues,
        date_to=date_to,
        limit=max_events,
    )
    milestones = repo.get_milestone_events(
        election_id, limit=max_events, date_to=date_to,
    )

    recent_ids = {event['event_id'] for event in recent}
    for event in historical:
        if event['event_id'] in recent_ids:
            continue
        if len(recent) < max_events:
            recent.append(event)
            recent_ids.add(event['event_id'])

    return {
        'snapshot': snapshot,
        'recent_events': recent[:max_events],
        'milestones': milestones,
        'context_query': {
            'election_id': election_id,
            'query_text': query_text,
            'actors': actors,
            'issues': issues,
            'as_of': date_to,
            'recent_days': recent_days,
        },
    }
