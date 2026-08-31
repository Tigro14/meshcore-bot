"""Cross-path parity: FeedManager.format_message == BotDataViewer._format_feed_item.

Guarantees live posts and web preview stay in sync after the shared feed_format refactor.
"""

from __future__ import annotations

from configparser import ConfigParser
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from modules.feed_manager import FeedManager
from modules.web_viewer.app import BotDataViewer


def _shared_config(
    *,
    max_message_length: int = 130,
    shorten_urls: bool = False,
) -> ConfigParser:
    config = ConfigParser()
    config.add_section("Feed_Manager")
    config.set("Feed_Manager", "feed_manager_enabled", "false")
    config.set("Feed_Manager", "max_message_length", str(max_message_length))
    config.set("Feed_Manager", "shorten_urls", "true" if shorten_urls else "false")
    return config


@pytest.fixture
def shared_config():
    return _shared_config()


@pytest.fixture
def fm(shared_config, mock_logger):
    bot = Mock()
    bot.logger = mock_logger
    bot.config = shared_config
    bot.db_manager = Mock()
    bot.db_manager.db_path = "/dev/null"
    return FeedManager(bot)


@pytest.fixture
def preview(shared_config, mock_logger):
    """Bind the real BotDataViewer._format_feed_item without full Flask/DB init."""
    viewer = SimpleNamespace(config=shared_config, logger=mock_logger)
    viewer._format_feed_item = BotDataViewer._format_feed_item.__get__(
        viewer, SimpleNamespace
    )
    return viewer


def _item(**kwargs):
    base = {
        "title": "Roadwork ahead",
        "description": "Lane closed on I-5",
        "link": "https://example.com/alert/12345",
        "published": datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
        "raw": {},
    }
    base.update(kwargs)
    return base


@pytest.mark.parametrize(
    "fmt,item_kwargs,feed_name",
    [
        ("{title}", {}, "x"),
        ("{emoji}", {"emoji": "🔥"}, "emergency alerts"),
        ("{emoji}", {}, "emergency alerts"),
        ("{emoji}", {}, "news feed"),
        ("{title|truncate_hard:4}", {}, "x"),
        ("{title|substr:0,4}", {}, "x"),
        ("{title|truncate:8}", {}, "x"),
        ("{body}", {"description": "<p>Hello <b>world</b><br/>next</p>"}, "x"),
        ("{title}", {"title": "Hi\x00there"}, "x"),
        ("{raw.Priority}", {"raw": {"Priority": "High"}}, "x"),
        ("{raw.Detail|truncate:10}", {"raw": {"Detail": "a" * 50}}, "x"),
        ("{link}", {}, "x"),
        ("{emoji} {title|truncate:20} - {date}\n{link|truncate:40}", {}, "info"),
        ("X{body|auto}Y", {"description": "abcdefghijklmnop"}, "x"),
    ],
    ids=[
        "title",
        "emoji-override",
        "emoji-emergency",
        "emoji-news",
        "truncate-hard",
        "substr",
        "truncate",
        "html-body",
        "sanitize-control",
        "raw-field",
        "raw-truncate",
        "plain-link",
        "defaultish-format",
        "auto-budget",
    ],
)
def test_format_parity_fm_matches_preview(fm, preview, fmt, item_kwargs, feed_name):
    item = _item(**item_kwargs)
    feed = {"feed_name": feed_name, "output_format": fmt, "id": 1}
    live = fm.format_message(item, feed)
    prev = preview._format_feed_item(item, fmt, feed_name=feed_name)
    assert live == prev


def test_format_parity_shorten_urls_enabled(fm, preview, monkeypatch):
    fm.shorten_feed_urls = True
    preview.config.set("Feed_Manager", "shorten_urls", "true")
    monkeypatch.setattr(
        "modules.feed_format.shorten_url_sync",
        lambda *a, **k: "https://v.gd/xy",
    )
    item = _item()
    fmt = "{link}"
    live = fm.format_message(item, {"feed_name": "x", "output_format": fmt})
    prev = preview._format_feed_item(item, fmt, feed_name="x")
    assert live == prev == "https://v.gd/xy"


def test_format_parity_link_shorten_placeholder(fm, preview, monkeypatch):
    fm.shorten_feed_urls = False
    preview.config.set("Feed_Manager", "shorten_urls", "false")
    monkeypatch.setattr(
        "modules.feed_format.shorten_url_sync",
        lambda *a, **k: "https://v.gd/zz",
    )
    item = _item()
    fmt = "{link|shorten}"
    live = fm.format_message(item, {"feed_name": "x", "output_format": fmt})
    prev = preview._format_feed_item(item, fmt, feed_name="x")
    assert live == prev == "https://v.gd/zz"


def test_format_parity_max_message_length(mock_logger, monkeypatch):
    config = _shared_config(max_message_length=40)
    bot = Mock()
    bot.logger = mock_logger
    bot.config = config
    bot.db_manager = Mock()
    bot.db_manager.db_path = "/dev/null"
    manager = FeedManager(bot)

    preview = SimpleNamespace(config=config, logger=mock_logger)
    preview._format_feed_item = BotDataViewer._format_feed_item.__get__(
        preview, SimpleNamespace
    )

    item = _item(title="A" * 80, description="B" * 80)
    fmt = "{title} {body}"
    live = manager.format_message(item, {"feed_name": "x", "output_format": fmt})
    prev = preview._format_feed_item(item, fmt, feed_name="x")
    assert live == prev
    assert len(live) <= 40


def test_format_parity_relative_date_uses_same_clock(fm, preview):
    """Both paths must resolve {date} identically for the same published stamp."""
    published = datetime.now(timezone.utc) - timedelta(minutes=12)
    item = _item(published=published)
    fmt = "{date}"
    live = fm.format_message(item, {"feed_name": "x", "output_format": fmt})
    prev = preview._format_feed_item(item, fmt, feed_name="x")
    assert live == prev
    assert live == "12m ago"
