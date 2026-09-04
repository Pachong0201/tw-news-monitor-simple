"""HTML collectors for approved military-specialist list pages."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..models import Article
from ..time_utils import TAIPEI
from .base import BaseCollector


LTN_ARTICLE_RE = re.compile(
    r"^https://def\.ltn\.com\.tw/article/breakingnews/\d+/?(?:\?.*)?$",
    re.IGNORECASE,
)
NOWNEWS_ARTICLE_RE = re.compile(
    r"^https://www\.nownews\.com/news/\d+/?(?:\?.*)?$",
    re.IGNORECASE,
)
MNA_ARTICLE_RE = re.compile(
    r"^https://mna\.mnd\.gov\.tw/news/detail/\?UserKey=[0-9a-f-]+$",
    re.IGNORECASE,
)
FULL_LTN_DATE_RE = re.compile(r"(\d{4}/\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2})")
TIME_ONLY_RE = re.compile(r"(?<!\d)(\d{1,2}:\d{2})(?!\d)")
ROC_DATE_RE = re.compile(r"民[國国]\s*(\d{2,3})年\s*(\d{1,2})月\s*(\d{1,2})日")


def _now_taipei() -> datetime:
    return datetime.now(TAIPEI)


def _response_status(response) -> int:
    return int(getattr(response, "status_code", 0) or 0)


def _article(
    collector: BaseCollector,
    *,
    title: str,
    url: str,
    published_at: datetime | None,
    fetched_at: datetime,
    position: int,
    summary: str | None = None,
) -> Article:
    return Article(
        source_id=collector.source_id,
        source_name=collector.source_name,
        category=collector.category,
        title=title,
        url=collector.normalize_url(url),
        published_at=published_at,
        fetched_at=fetched_at,
        position=position,
        summary=summary,
        summary_source="meta" if summary else None,
    )


def _parse_ltn_date(text: str, now: datetime) -> datetime | None:
    full = FULL_LTN_DATE_RE.search(text)
    if full:
        try:
            return datetime.strptime(full.group(1), "%Y/%m/%d %H:%M").replace(
                tzinfo=TAIPEI
            )
        except ValueError:
            return None
    clock = TIME_ONLY_RE.search(text)
    if clock:
        try:
            parsed = datetime.strptime(clock.group(1), "%H:%M")
            return now.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
        except ValueError:
            return None
    return None


def parse_roc_date(value: str) -> datetime | None:
    """Convert a ROC calendar date such as 民国115年09月04日 to Taipei time."""

    match = ROC_DATE_RE.search(value or "")
    if not match:
        return None
    try:
        return datetime(
            int(match.group(1)) + 1911,
            int(match.group(2)),
            int(match.group(3)),
            tzinfo=TAIPEI,
        )
    except ValueError:
        return None


class LtnMilitaryCollector(BaseCollector):
    """Collect list metadata from Liberty Times' dedicated military site."""

    def collect(self) -> list[Article]:
        response = self.get_with_retry(self.url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        now = _now_taipei()
        articles: list[Article] = []
        seen: set[str] = set()
        for anchor in soup.find_all("a", href=True):
            href = urljoin(self.url, anchor.get("href", "").strip())
            if not LTN_ARTICLE_RE.match(href):
                continue
            title_node = anchor.find(["h2", "h3", "h4"])
            title = (
                title_node.get_text(" ", strip=True)
                if title_node is not None
                else str(anchor.get("title") or anchor.get_text(" ", strip=True)).strip()
            )
            if not title:
                continue
            normalized = self.normalize_url(href)
            if normalized in seen:
                continue
            seen.add(normalized)
            container = anchor.find_parent("li") or anchor.parent
            context = container.get_text(" ", strip=True) if container else anchor.get_text(" ", strip=True)
            articles.append(
                _article(
                    self,
                    title=title,
                    url=href,
                    published_at=_parse_ltn_date(context, now),
                    fetched_at=now,
                    position=len(articles) + 1,
                )
            )
            if len(articles) >= self.MAX_ITEMS:
                break
        valid = bool(articles)
        self.mark_outcome(
            http_status=_response_status(response),
            schema_valid=valid,
            item_count=len(articles),
            error_code=None if valid else "schema",
        )
        return articles


class NownewsMilitaryCollector(BaseCollector):
    """Collect NOWnews military list pages with a bounded old-news stop."""

    DEFAULT_MAX_PAGES = 3
    DEFAULT_STOP_AFTER_HOURS = 72

    def _page_url(self, page_index: int) -> str:
        if page_index == 0:
            return self.url
        return f"{self.url.rstrip('/')}/page/{page_index}/"

    def _parse_page(self, html: str, fetched_at: datetime) -> tuple[list[Article], bool]:
        soup = BeautifulSoup(html, "html.parser")
        list_node = soup.select_one("ul#ulNewsList")
        if list_node is None:
            return [], False
        parsed: list[Article] = []
        for item in list_node.select("li.item"):
            anchor = item.find("a", href=True)
            if anchor is None:
                continue
            href = urljoin(self.url, anchor.get("href", "").strip())
            if not NOWNEWS_ARTICLE_RE.match(href):
                continue
            title_node = item.select_one("h3.title")
            title = title_node.get_text(" ", strip=True) if title_node else ""
            if not title:
                continue
            published_at = None
            time_node = item.find("time")
            raw_time = str(time_node.get("datetime") or time_node.get_text(strip=True)) if time_node else ""
            if raw_time:
                try:
                    published_at = datetime.fromisoformat(raw_time).replace(tzinfo=TAIPEI)
                except ValueError:
                    published_at = None
            parsed.append(
                _article(
                    self,
                    title=title,
                    url=href,
                    published_at=published_at,
                    fetched_at=fetched_at,
                    position=0,
                )
            )
        return parsed, True

    def collect(self) -> list[Article]:
        now = _now_taipei()
        max_pages = max(1, min(int(self.source.get("max_pages", self.DEFAULT_MAX_PAGES)), 10))
        stop_after = max(1, int(self.source.get("stop_after_hours", self.DEFAULT_STOP_AFTER_HOURS)))
        cutoff = now - timedelta(hours=stop_after)
        articles: list[Article] = []
        seen: set[str] = set()
        first_status = 0
        first_schema_valid = False
        reached_old = False

        for page_index in range(max_pages):
            response = self.get_with_retry(self._page_url(page_index))
            response.raise_for_status()
            if page_index == 0:
                first_status = _response_status(response)
            page_articles, schema_valid = self._parse_page(response.text, now)
            if page_index == 0:
                first_schema_valid = schema_valid
            if not schema_valid:
                if page_index == 0:
                    self.mark_outcome(
                        http_status=first_status,
                        schema_valid=False,
                        item_count=0,
                        error_code="schema",
                    )
                    return []
                break
            for article in page_articles:
                if article.published_at is not None and article.published_at < cutoff:
                    reached_old = True
                    break
                if article.url in seen:
                    continue
                seen.add(article.url)
                article.position = len(articles) + 1
                articles.append(article)
                if len(articles) >= self.MAX_ITEMS:
                    break
            if reached_old or len(articles) >= self.MAX_ITEMS or not page_articles:
                break

        valid = first_schema_valid and bool(articles)
        self.mark_outcome(
            http_status=first_status,
            schema_valid=valid,
            item_count=len(articles),
            error_code=None if valid else "schema",
        )
        return articles


class MNAMilitaryCollector(BaseCollector):
    """Collect list metadata from the ROC Ministry of National Defense MNA."""

    def collect(self) -> list[Article]:
        response = self.get_with_retry(self.url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        now = _now_taipei()
        articles: list[Article] = []
        seen: set[str] = set()
        for anchor in soup.select('a[href*="/news/detail/?UserKey="]'):
            href = urljoin(self.url, anchor.get("href", "").strip())
            if not MNA_ARTICLE_RE.match(href):
                continue
            title_node = anchor.select_one("div.title")
            title = (
                title_node.get_text(" ", strip=True)
                if title_node is not None
                else str(anchor.get("title") or "").strip()
            )
            if not title:
                continue
            normalized = self.normalize_url(href)
            if normalized in seen:
                continue
            seen.add(normalized)
            summary_node = anchor.select_one("div.summary")
            summary = summary_node.get_text(" ", strip=True) if summary_node else None
            time_node = anchor.select_one("span.time")
            published_at = parse_roc_date(time_node.get_text(" ", strip=True) if time_node else "")
            articles.append(
                _article(
                    self,
                    title=title,
                    url=href,
                    published_at=published_at,
                    fetched_at=now,
                    position=len(articles) + 1,
                    summary=summary,
                )
            )
            if len(articles) >= self.MAX_ITEMS:
                break
        valid = bool(articles)
        self.mark_outcome(
            http_status=_response_status(response),
            schema_valid=valid,
            item_count=len(articles),
            error_code=None if valid else "schema",
        )
        return articles
