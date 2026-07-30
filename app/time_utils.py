from datetime import datetime
from zoneinfo import ZoneInfo

TAIPEI = ZoneInfo("Asia/Taipei")


def normalize_published_at(
    value: datetime | None,
    *,
    assumed_timezone: ZoneInfo | None = None,
) -> datetime | None:
    """Normalize published_at to timezone-aware Asia/Taipei.

    - None returns None
    - Aware converts via astimezone(Asia/Taipei)
    - Naive with assumed_timezone applies replace(tzinfo)
    - Naive without assumed_timezone returns None
    - Does not modify the input object
    """
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(TAIPEI)
    if assumed_timezone is not None:
        return value.replace(tzinfo=assumed_timezone)
    return None
