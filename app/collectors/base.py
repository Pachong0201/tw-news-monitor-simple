from abc import ABC, abstractmethod
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import httpx

from ..models import Article


TRACKING_PARAMS = frozenset({
    "from",
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_content",
    "fbclid",
    "gclid",
})


class BaseCollector(ABC):
    """Base class for news collectors."""

    MAX_ITEMS = 20
    TIMEOUT = 15.0
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    def __init__(self, source: dict):
        self.source = source
        self._client: httpx.Client | None = None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.TIMEOUT,
                headers={"User-Agent": self.USER_AGENT},
                follow_redirects=True,
            )
        return self._client

    @abstractmethod
    def collect(self) -> list[Article]:
        """Collect articles from the source. Returns at most MAX_ITEMS."""

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    @staticmethod
    def normalize_url(url: str) -> str:
        """Normalize URL for deduplication.

        Strips tracking parameters (from, utm_*, fbclid, gclid),
        removes fragments, lowercases host and path, removes
        trailing slashes, and sorts remaining query parameters
        for stable dedup keys.
        """
        parsed = urlparse(url.strip())
        normalized = parsed._replace(
            scheme=parsed.scheme.lower(),
            netloc=parsed.netloc.lower(),
            fragment="",
        )
        path = normalized.path.rstrip("/")
        path = path.lower()
        normalized = normalized._replace(path=path)
        # Strip tracking parameters from query string
        if normalized.query:
            params = parse_qs(normalized.query, keep_blank_values=True)
            filtered = {
                k: v
                for k, v in params.items()
                if k.lower() not in TRACKING_PARAMS
            }
            if filtered:
                sorted_query = urlencode(sorted(filtered.items()), doseq=True)
                normalized = normalized._replace(query=sorted_query)
            else:
                normalized = normalized._replace(query="")
        return urlunparse(normalized)

    @property
    def source_id(self) -> str:
        return self.source["id"]

    @property
    def source_name(self) -> str:
        return self.source["name"]

    @property
    def category(self) -> str:
        return self.source["category"]

    @property
    def url(self) -> str:
        return self.source["url"]
