"""Unit tests for modules.feed_format (shared FeedManager + web preview path)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from modules.feed_format import (
    apply_feed_field_function,
    clean_feed_html_body,
    feed_format_auto_slots,
    format_feed_message,
    format_relative_timestamp,
    sort_feed_items,
    truncate_to_budget,
)


class TestFormatRelativeTimestamp:
    def test_none_returns_empty(self):
        assert format_relative_timestamp(None) == ""

    def test_recent_minutes(self):
        published = datetime.now(timezone.utc) - timedelta(minutes=5)
        assert format_relative_timestamp(published) == "5m ago"

    def test_now(self):
        published = datetime.now(timezone.utc)
        assert format_relative_timestamp(published) == "now"


class TestAutoSlotsAndBudget:
    def test_auto_slots_finds_field(self):
        slots = feed_format_auto_slots("{emoji} {body|auto} {date}")
        assert len(slots) == 1
        assert slots[0][2] == "body"

    def test_truncate_to_budget_ellipsis(self):
        assert truncate_to_budget("abcdef", 5) == "ab..."

    def test_truncate_to_budget_short(self):
        assert truncate_to_budget("hi", 10) == "hi"


class TestCleanHtml:
    def test_strips_tags(self):
        assert "<b>" not in clean_feed_html_body("<b>Hello</b>")
        assert "Hello" in clean_feed_html_body("<b>Hello</b>")

    def test_br_to_newline(self):
        assert "\n" in clean_feed_html_body("a<br>b")


class TestApplyFieldFunction:
    def test_truncate(self):
        assert apply_feed_field_function("Hello World", "truncate:5") == "Hello..."

    def test_truncate_hard(self):
        assert apply_feed_field_function("Hello World", "truncate_hard:5") == "Hello"

    def test_substr(self):
        assert apply_feed_field_function("Hello World", "substr:6,5") == "World"


class TestSortFeedItems:
    def test_sort_desc_by_title(self):
        items = [
            {"title": "a", "raw": {}},
            {"title": "c", "raw": {}},
            {"title": "b", "raw": {}},
        ]
        sorted_items = sort_feed_items(items, {"field": "title", "order": "desc"})
        assert [i["title"] for i in sorted_items] == ["c", "b", "a"]


class TestFormatFeedMessage:
    def test_basic_title(self):
        msg = format_feed_message(
            {"title": "Hello", "description": ""},
            "{title}",
        )
        assert msg == "Hello"

    def test_title_unescapes_html_entities(self):
        msg = format_feed_message(
            {"title": "Foo &amp; Bar&#39;s", "description": ""},
            "{title}",
        )
        assert msg == "Foo & Bar's"

    def test_sanitize_strips_control_chars(self):
        msg = format_feed_message(
            {"title": "Hi\x00there", "description": ""},
            "{title}",
        )
        assert "\x00" not in msg
        assert "Hi" in msg and "there" in msg

    def test_emoji_heuristic(self):
        msg = format_feed_message(
            {"title": "x", "description": ""},
            "{emoji}",
            feed_name="emergency alerts",
        )
        assert msg == "🚨"

    def test_emoji_field_overrides(self):
        msg = format_feed_message(
            {"title": "x", "description": "", "emoji": "🔥"},
            "{emoji}",
            feed_name="emergency alerts",
        )
        assert msg == "🔥"

    def test_auto_fills_budget(self):
        msg = format_feed_message(
            {"title": "T", "description": "abcdefghijklmnop"},
            "X{body|auto}Y",
            max_message_length=10,
        )
        assert msg.startswith("X")
        assert msg.endswith("Y")
        assert len(msg) <= 10

    @patch("modules.feed_format.shorten_url_sync", return_value="https://v.gd/x")
    def test_shorten_feed_urls_plain_link(self, _mock_shorten):
        msg = format_feed_message(
            {"title": "t", "description": "", "link": "https://example.com/long"},
            "{link}",
            shorten_feed_urls=True,
            config=object(),
        )
        assert msg == "https://v.gd/x"

    @patch("modules.feed_format.shorten_url_sync", return_value="https://v.gd/x")
    def test_shorten_disabled_keeps_long_link(self, _mock_shorten):
        msg = format_feed_message(
            {"title": "t", "description": "", "link": "https://example.com/long"},
            "{link}",
            shorten_feed_urls=False,
        )
        assert msg == "https://example.com/long"
