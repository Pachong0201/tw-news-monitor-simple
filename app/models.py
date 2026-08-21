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
    # 国际媒体免费监测层 Phase I（2026-08-13）：
    # source_name 复用为 canonical publisher（如 "Reuters"），不再新增 publisher 字段。
    section: str | None = None
    language: str | None = None
    # access_level 合法值：public / metadata_only / newsletter
    access_level: str | None = None
