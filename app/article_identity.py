import re
from urllib.parse import urlsplit
from .collectors.base import BaseCollector

UDN_STORY_PATTERN = re.compile(r"^/news/story/[^/]+/(\d+)/?$")
CNA_ARTICLE_PATTERN = re.compile(r"^/news/([a-z]+)/(\d+)\.aspx$")
LTN_PATH_PATTERN = re.compile(r"^/news/[^/]+/[^/]+/\d+$")
NEWTALK_PATTERN = re.compile(r"^/news/view/\d{4}-\d{2}-\d{2}/(\d+)$")
STORM_PATTERN = re.compile(r"^/article/(\d+)")
EY_PATTERN = re.compile(r"^/page/[0-9a-f]+//([0-9a-f-]+)")

def article_identity_key(url: str) -> str:
    normalized = BaseCollector.normalize_url(url)
    parts = urlsplit(normalized)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host == "udn.com":
        match = UDN_STORY_PATTERN.fullmatch(parts.path)
        if match:
            return "udn:" + match.group(1)
    if host == "cna.com.tw":
        match = CNA_ARTICLE_PATTERN.fullmatch(parts.path)
        if match:
            return "cna:" + match.group(1) + ":" + match.group(2)
    if host == "news.ltn.com.tw":
        match = LTN_PATH_PATTERN.fullmatch(parts.path)
        if match:
            return "ltn:" + parts.path.lower().strip("/")
    if host in ("newtalk.tw", "www.newtalk.tw"):
        match = NEWTALK_PATTERN.fullmatch(parts.path)
        if match:
            return "newtalk:" + match.group(1)
    if host == "storm.mg":
        match = STORM_PATTERN.fullmatch(parts.path)
        if match:
            return "storm:" + match.group(1)
    if host == "ey.gov.tw":
        match = EY_PATTERN.fullmatch(parts.path)
        if match:
            return "ey:" + match.group(1)
    return "url:" + normalized

def deduplicate_articles_by_identity(articles):
    seen = set()
    unique = []
    dups = []
    for article in articles:
        if not article.url:
            unique.append(article)
            continue
        key = article_identity_key(article.url)
        if key in seen:
            dups.append(article)
        else:
            seen.add(key)
            unique.append(article)
    return unique, dups