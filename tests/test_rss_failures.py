from unittest.mock import Mock

import httpx
import pytest

from app.collectors.rss import RSSCollector


def make_collector():
    return RSSCollector({
        "id": "rss-test",
        "name": "RSS 测试",
        "category": "politics",
        "collector": "rss",
        "url": "https://example.com/feed.xml",
    })


def test_http_error_is_not_reported_as_empty_feed():
    collector = make_collector()
    response = Mock()
    response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "503", request=Mock(), response=Mock(status_code=503),
    )
    collector.client.get = Mock(return_value=response)

    with pytest.raises(httpx.HTTPStatusError):
        collector.collect()


def test_malformed_feed_without_usable_entries_raises():
    collector = make_collector()
    response = Mock(text="this is not an RSS document")
    response.raise_for_status.return_value = None
    collector.client.get = Mock(return_value=response)

    with pytest.raises(ValueError, match="Invalid RSS feed|no usable entries"):
        collector.collect()


def test_valid_empty_feed_is_allowed():
    collector = make_collector()
    response = Mock(text="<?xml version='1.0'?><rss><channel></channel></rss>")
    response.raise_for_status.return_value = None
    collector.client.get = Mock(return_value=response)

    assert collector.collect() == []
