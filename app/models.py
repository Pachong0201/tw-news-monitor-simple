from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class Article:
    source_id: str
    source_name: str
    category: str
    title: str
    url: str
    published_at: datetime | None
    fetched_at: datetime
    position: int
    summary: str | None = None
    summary_source: str | None = None
    summary_attempted_at: datetime | None = None
