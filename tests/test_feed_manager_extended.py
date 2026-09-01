"""Extended FeedManager tests: sort, format_message, mocked RSS/API fetch, queue processing."""

from __future__ import annotations

import asyncio
import json
from configparser import ConfigParser
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from modules.db_manager import DBManager
from modules.feed_manager import FeedManager


def _feed_manager_bot(mock_logger, db_path: str):
    bot = Mock()
    bot.logger = mock_logger
    bot.config = ConfigParser()
    bot.config.add_section("Feed_Manager")
    bot.config.set("Feed_Manager", "feed_manager_enabled", "false")
    bot.config.set("Feed_Manager", "max_message_length", "200")
    bot.db_manager = DBManager(bot, db_path)
    return bot


@pytest.fixture
def fm_with_db(mock_logger, tmp_path):
    """FeedManager backed by a real file SQLite DB (feed tables from DBManager)."""
    db_path = str(tmp_path / "feeds.db")
    bot = _feed_manager_bot(mock_logger, db_path)
    return FeedManager(bot)


def _seed_feed_subscription(db_manager: DBManager, feed_id: int = 1, channel_name: str = "general") -> None:
    """Insert a minimal feed_subscriptions row so feed_activity / feed_errors FK inserts succeed."""
    with db_manager.connection() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO feed_subscriptions
            (id, feed_type, feed_url, channel_name, enabled)
            VALUES (?, 'rss', 'http://example.com/feed.xml', ?, 1)
            """,
            (feed_id, channel_name),
        )
        conn.commit()


def _fake_aiohttp_response(*, text_body: str | None = None, json_body: dict | list | None = None):
    """Build a minimal aiohttp response used by the safe request helper."""
    resp = Mock()
    resp.status = 200
    body = text_body.encode() if text_body is not None else json.dumps(json_body).encode()
    content_type = 'application/rss+xml' if text_body is not None else 'application/json'
    resp.headers = {'Content-Type': content_type}
    resp.release = Mock()

    async def chunks():
        yield body

    resp.content.iter_chunked = Mock(return_value=chunks())
    if text_body is not None:
        resp.text = AsyncMock(return_value=text_body)
    if json_body is not None:
        resp.json = AsyncMock(return_value=json_body)
    return resp


class TestSortItems:
    def test_sort_by_published_desc(self, fm_with_db):
        fm = fm_with_db
        older = datetime(2020, 1, 1, tzinfo=timezone.utc)
        newer = datetime(2025, 6, 1, tzinfo=timezone.utc)
        items = [
            {"id": "a", "title": "old", "published": older, "raw": {}},
            {"id": "b", "title": "new", "published": newer, "raw": {}},
        ]
        out = fm._sort_items(items, {"field": "published", "order": "desc"})
        assert [x["id"] for x in out] == ["b", "a"]

    def test_sort_by_raw_numeric_timestamp_asc(self, fm_with_db):
        fm = fm_with_db
        items = [
            {"id": "2", "title": "t2", "raw": {"t": 200.0}, "published": None},
            {"id": "1", "title": "t1", "raw": {"t": 100.0}, "published": None},
        ]
        out = fm._sort_items(items, {"field": "raw.t", "order": "asc"})
        assert [x["id"] for x in out] == ["1", "2"]

    def test_sort_empty_field_returns_unchanged(self, fm_with_db):
        fm = fm_with_db
        items = [{"id": "x", "title": "a"}]
        out = fm._sort_items(items, {"field": "", "order": "desc"})
        assert out == items


class TestFormatMessage:
    def test_basic_placeholders(self, fm_with_db):
        fm = fm_with_db
        now = datetime.now(timezone.utc)
        feed = {"output_format": "{emoji} {title}\n{link}\n{date}", "feed_name": "news"}
        item = {
            "title": "Hello",
            "link": "https://ex.com/a",
            "description": "",
            "published": now,
            "raw": {},
        }
        msg = fm.format_message(item, feed)
        assert "Hello" in msg
        assert "https://ex.com/a" in msg
        assert "📢" in msg or "ℹ️" in msg  # emoji from feed_name or default

    def test_link_dict_href_coerced_like_feedparser(self, fm_with_db):
        fm = fm_with_db
        fm.shorten_feed_urls = False
        feed = {"output_format": "{link}", "feed_name": "x"}
        item = {
            "title": "t",
            "link": {"href": "https://ex.com/from-dict"},
            "description": "",
            "published": None,
            "raw": {},
        }
        assert fm.format_message(item, feed) == "https://ex.com/from-dict"

    def test_feed_name_null_from_db_does_not_crash(self, fm_with_db):
        fm = fm_with_db
        feed = {"output_format": "{emoji}{title}", "feed_name": None}
        item = {
            "title": "Hi",
            "link": "",
            "description": "",
            "published": None,
            "raw": {},
        }
        msg = fm.format_message(item, feed)
        assert "Hi" in msg

    def test_strips_br_and_html_from_body(self, fm_with_db):
        fm = fm_with_db
        feed = {"output_format": "{body}"}
        item = {
            "title": "t",
            "description": 'Line1<br/>Line2<p>Para</p>',
            "published": None,
            "raw": {},
        }
        msg = fm.format_message(item, feed)
        assert "<br" not in msg.lower()
        assert "<p" not in msg.lower()
        assert "Line1" in msg and "Line2" in msg

    def test_raw_field_with_truncate(self, fm_with_db):
        fm = fm_with_db
        feed = {"output_format": "{raw.Status|truncate:4}"}
        item = {
            "title": "t",
            "description": "",
            "published": None,
            "raw": {"Status": "open"},
        }
        assert fm.format_message(item, feed) == "open"

    def test_max_message_length_truncates(self, fm_with_db):
        fm = fm_with_db
        fm.max_message_length = 20
        feed = {"output_format": "{title}"}
        item = {"title": "x" * 40, "description": "", "published": None, "raw": {}}
        msg = fm.format_message(item, feed)
        assert len(msg) <= 23  # 20 + "..."
        assert msg.endswith("...")

    def test_shorten_urls_disabled_keeps_original_link(self, fm_with_db):
        fm = fm_with_db
        fm.shorten_feed_urls = False
        feed = {"output_format": "{link}", "feed_name": "x"}
        item = {
            "title": "t",
            "link": "https://example.com/long/path",
            "description": "",
            "published": None,
            "raw": {},
        }
        with patch("modules.feed_format.shorten_url_sync") as mock_shorten:
            msg = fm.format_message(item, feed)
        mock_shorten.assert_not_called()
        assert msg == "https://example.com/long/path"

    def test_shorten_urls_replaces_link_when_shortener_returns_value(self, fm_with_db):
        fm = fm_with_db
        fm.shorten_feed_urls = True
        feed = {"output_format": "{link}", "feed_name": "x"}
        item = {
            "title": "t",
            "link": "https://example.com/long/path",
            "description": "",
            "published": None,
            "raw": {},
        }
        with patch(
            "modules.feed_format.shorten_url_sync",
            return_value="https://v.gd/abc",
        ) as mock_shorten:
            msg = fm.format_message(item, feed)
        mock_shorten.assert_called_once()
        assert msg == "https://v.gd/abc"

    def test_shorten_urls_keeps_original_when_shortener_fails(self, fm_with_db):
        fm = fm_with_db
        fm.shorten_feed_urls = True
        feed = {"output_format": "{link}", "feed_name": "x"}
        item = {
            "title": "t",
            "link": "https://example.com/long/path",
            "description": "",
            "published": None,
            "raw": {},
        }
        with patch("modules.feed_format.shorten_url_sync", return_value=""):
            msg = fm.format_message(item, feed)
        assert msg == "https://example.com/long/path"

    def test_link_shorten_placeholder_without_global(self, fm_with_db):
        fm = fm_with_db
        fm.shorten_feed_urls = False
        feed = {"output_format": "{link|shorten}", "feed_name": "x"}
        item = {
            "title": "t",
            "link": "https://example.com/long/path",
            "description": "",
            "published": None,
            "raw": {},
        }
        with patch(
            "modules.feed_format.shorten_url_sync",
            return_value="https://v.gd/x",
        ) as mock_shorten:
            msg = fm.format_message(item, feed)
        mock_shorten.assert_called_once()
        assert msg == "https://v.gd/x"

    def test_link_shorten_with_global_only_one_shorten_call(self, fm_with_db):
        fm = fm_with_db
        fm.shorten_feed_urls = True
        feed = {"output_format": "{link|shorten}", "feed_name": "x"}
        item = {
            "title": "t",
            "link": "https://example.com/long/path",
            "description": "",
            "published": None,
            "raw": {},
        }
        with patch(
            "modules.feed_format.shorten_url_sync",
            return_value="https://v.gd/x",
        ) as mock_shorten:
            msg = fm.format_message(item, feed)
        mock_shorten.assert_called_once()
        assert msg == "https://v.gd/x"

    def test_link_shorten_then_truncate_chain(self, fm_with_db):
        fm = fm_with_db
        fm.shorten_feed_urls = False
        feed = {"output_format": "{link|shorten|truncate:12}", "feed_name": "x"}
        item = {
            "title": "t",
            "link": "https://example.com/long/path",
            "description": "",
            "published": None,
            "raw": {},
        }
        with patch(
            "modules.feed_format.shorten_url_sync",
            return_value="https://v.gd/abcdefghijklmnop",
        ) as mock_shorten:
            msg = fm.format_message(item, feed)
        mock_shorten.assert_called_once()
        assert len(msg) <= 15  # 12 + "..."
        assert msg.endswith("...")

    def test_title_auto_fits_max_message_length(self, fm_with_db):
        fm = fm_with_db
        fm.max_message_length = 40
        feed = {"output_format": "{emoji} {title|auto}\nD", "feed_name": "x"}
        item = {
            "title": "A" * 100,
            "link": "",
            "description": "",
            "published": None,
            "raw": {},
        }
        msg = fm.format_message(item, feed)
        assert len(msg) <= 40

    def test_auto_when_prefix_exceeds_max_uses_final_truncation(self, fm_with_db):
        fm = fm_with_db
        fm.max_message_length = 20
        feed = {"output_format": "{title}{title|auto}", "feed_name": "x"}
        item = {
            "title": "B" * 25,
            "link": "",
            "description": "",
            "published": None,
            "raw": {},
        }
        msg = fm.format_message(item, feed)
        assert len(msg) <= 23  # max_message_length + "..."

    def test_multiple_auto_warning_second_renders_empty(self, fm_with_db, mock_logger):
        fm = fm_with_db
        fm.max_message_length = 120
        feed = {
            "output_format": "{title|auto}|X|{body|auto}",
            "feed_name": "x",
            "id": 99,
        }
        item = {
            "title": "Hello",
            "link": "",
            "description": "ignored",
            "published": None,
            "raw": {},
        }
        msg = fm.format_message(item, feed)
        mock_logger.warning.assert_called()
        assert msg == "Hello|X|"

    def test_body_auto_multiline(self, fm_with_db):
        fm = fm_with_db
        fm.max_message_length = 30
        feed = {"output_format": "H\n{body|auto}\nT", "feed_name": "x"}
        item = {
            "title": "t",
            "link": "",
            "description": "line1\nline2\n" + "Z" * 50,
            "published": None,
            "raw": {},
        }
        msg = fm.format_message(item, feed)
        assert len(msg) <= 30
        assert msg.startswith("H\n")
        assert msg.endswith("\nT")


class TestProcessRssFeed:
    @pytest.mark.asyncio
    async def test_returns_items_from_xml(self, fm_with_db):
        fm = fm_with_db
        rss = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>T</title>
<item><title>One</title><link>http://e/1</link><guid>g1</guid><description>D1</description></item>
<item><title>Two</title><link>http://e/2</link><guid>g2</guid><description>D2</description></item>
</channel></rss>"""
        ctx = _fake_aiohttp_response(text_body=rss)
        fm.session = Mock()
        fm.session.request = AsyncMock(return_value=ctx)
        fm.session.closed = False
        fm._url_policy.validate_async = AsyncMock(return_value=True)

        feed = {"id": 1, "feed_url": "http://example.com/feed.xml"}
        items = await fm.process_rss_feed(feed)
        titles = {i["title"] for i in items}
        assert titles == {"One", "Two"}

    @pytest.mark.asyncio
    async def test_skips_already_processed(self, fm_with_db):
        fm = fm_with_db
        rss = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>T</title>
<item><title>Old</title><link>http://e/1</link><guid>g1</guid><description></description></item>
<item><title>New</title><link>http://e/2</link><guid>g2</guid><description></description></item>
</channel></rss>"""
        ctx = _fake_aiohttp_response(text_body=rss)
        fm.session = Mock()
        fm.session.request = AsyncMock(return_value=ctx)
        fm.session.closed = False
        fm._url_policy.validate_async = AsyncMock(return_value=True)

        _seed_feed_subscription(fm.bot.db_manager, feed_id=1)
        with fm.bot.db_manager.connection() as conn:
            conn.execute(
                "INSERT INTO feed_activity (feed_id, item_id, item_title, message_sent) VALUES (?,?,?,1)",
                (1, "g1", "Old"),
            )
            conn.commit()

        feed = {"id": 1, "feed_url": "http://example.com/feed.xml"}
        items = await fm.process_rss_feed(feed)
        assert len(items) == 1
        assert items[0]["title"] == "New"

    @pytest.mark.asyncio
    async def test_skips_item_already_waiting_in_queue(self, fm_with_db):
        fm = fm_with_db
        rss = """<rss version="2.0"><channel>
<item><title>Queued</title><guid>queued-1</guid></item>
<item><title>New</title><guid>new-1</guid></item>
</channel></rss>"""
        fm.session = Mock()
        fm.session.request = AsyncMock(return_value=_fake_aiohttp_response(text_body=rss))
        fm.session.closed = False
        fm._url_policy.validate_async = AsyncMock(return_value=True)
        _seed_feed_subscription(fm.bot.db_manager, feed_id=1)
        with fm.bot.db_manager.connection() as conn:
            conn.execute(
                """INSERT INTO feed_message_queue
                   (feed_id, channel_name, message, item_id, item_title, priority)
                   VALUES (1, 'general', 'pending', 'queued-1', 'Queued', 0)"""
            )
            conn.commit()

        items = await fm.process_rss_feed(
            {"id": 1, "feed_url": "http://example.com/feed.xml"}
        )

        assert [item["id"] for item in items] == ["new-1"]


class TestFeedConcurrencyAndLimits:
    @pytest.mark.asyncio
    async def test_concurrent_polls_for_same_feed_are_serialized(self, fm_with_db):
        fm = fm_with_db
        active = 0
        max_active = 0

        async def poll_once(_feed):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1

        fm._poll_feed_locked = AsyncMock(side_effect=poll_once)
        feed = {"id": 42}
        await asyncio.gather(fm.poll_feed(feed), fm.poll_feed(feed))

        assert max_active == 1

    @pytest.mark.asyncio
    async def test_concurrent_queue_attempts_insert_one_row(self, fm_with_db):
        fm = fm_with_db
        _seed_feed_subscription(fm.bot.db_manager, feed_id=1)
        feed = {"id": 1, "channel_name": "general"}
        item = {"id": "concurrent-id", "title": "Concurrent"}

        results = await asyncio.gather(
            *[
                asyncio.to_thread(fm._queue_feed_message, feed, item, f"message-{i}")
                for i in range(6)
            ]
        )

        assert sum(results) == 1
        with fm.bot.db_manager.connection() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM feed_message_queue WHERE item_id = 'concurrent-id'"
            ).fetchone()[0] == 1

    @pytest.mark.asyncio
    async def test_chunked_decompressed_body_over_limit_is_rejected(self, fm_with_db):
        fm = fm_with_db
        fm.max_response_bytes = 5
        response = Mock()
        response.headers = {"Content-Type": "application/rss+xml"}

        async def chunks():
            yield b"123"
            yield b"456"

        response.content.iter_chunked = Mock(return_value=chunks())
        with pytest.raises(ValueError, match="exceeds 5 byte limit"):
            await fm._read_limited_response(response, "rss")

    @pytest.mark.asyncio
    async def test_oversized_content_length_is_rejected_before_read(self, fm_with_db):
        fm = fm_with_db
        fm.max_response_bytes = 5
        response = Mock()
        response.headers = {
            "Content-Type": "application/json",
            "Content-Length": "6",
        }
        with pytest.raises(ValueError, match="exceeds 5 byte limit"):
            await fm._read_limited_response(response, "api")

    @pytest.mark.asyncio
    async def test_unrelated_content_type_is_rejected(self, fm_with_db):
        response = Mock()
        response.headers = {"Content-Type": "image/png"}
        with pytest.raises(ValueError, match="Unexpected RSS content type"):
            await fm_with_db._read_limited_response(response, "rss")

    @pytest.mark.asyncio
    async def test_rate_limit_serializes_same_normalized_host(self, fm_with_db):
        fm = fm_with_db
        fm.rate_limit_seconds = 0.02
        assert fm._normalized_host("HTTPS://Example.COM.:443/a") == "example.com"
        started = asyncio.get_running_loop().time()
        await asyncio.gather(
            fm._wait_for_rate_limit("example.com"),
            fm._wait_for_rate_limit("example.com"),
        )
        assert asyncio.get_running_loop().time() - started >= 0.015


class TestProcessApiFeed:
    @pytest.mark.asyncio
    async def test_get_parses_items_path(self, fm_with_db):
        fm = fm_with_db
        payload = {
            "data": {
                "rows": [
                    {"id": "10", "name": "Alpha", "created_at": 1700000000},
                ]
            }
        }
        ctx = _fake_aiohttp_response(json_body=payload)
        fm.session = Mock()
        fm.session.request = AsyncMock(return_value=ctx)
        fm.session.closed = False
        fm._url_policy.validate_async = AsyncMock(return_value=True)

        api_config = json.dumps(
            {
                "response_parser": {
                    "items_path": "data.rows",
                    "id_field": "id",
                    "title_field": "name",
                    "timestamp_field": "created_at",
                }
            }
        )
        feed = {"id": 2, "feed_url": "http://api.example.com/x", "api_config": api_config}
        items = await fm.process_api_feed(feed)
        assert len(items) == 1
        assert items[0]["id"] == "10"
        assert items[0]["title"] == "Alpha"

    @pytest.mark.asyncio
    async def test_post_json_body(self, fm_with_db):
        fm = fm_with_db
        payload = [{"id": "z", "title": "Zed", "created_at": 1600000000}]
        ctx = _fake_aiohttp_response(json_body=payload)
        fm.session = Mock()
        fm.session.request = AsyncMock(return_value=ctx)
        fm.session.closed = False
        fm._url_policy.validate_async = AsyncMock(return_value=True)

        api_config = json.dumps(
            {
                "method": "POST",
                "body": {"q": 1},
                "response_parser": {
                    "items_path": "",
                    "id_field": "id",
                    "title_field": "title",
                    "timestamp_field": "created_at",
                },
            }
        )
        feed = {"id": 3, "feed_url": "http://api.example.com/post", "api_config": api_config}
        items = await fm.process_api_feed(feed)
        assert len(items) == 1
        assert items[0]["title"] == "Zed"

    @pytest.mark.asyncio
    async def test_caps_number_of_parsed_items(self, fm_with_db):
        fm = fm_with_db
        fm.max_parsed_items = 2
        payload = [
            {"id": str(i), "title": f"Item {i}", "created_at": 1600000000 + i}
            for i in range(5)
        ]
        fm.session = Mock()
        fm.session.request = AsyncMock(
            return_value=_fake_aiohttp_response(json_body=payload)
        )
        fm.session.closed = False
        fm._url_policy.validate_async = AsyncMock(return_value=True)

        items = await fm.process_api_feed(
            {"id": 4, "feed_url": "http://api.example.com/items", "api_config": "{}"}
        )

        assert len(items) == 2


class TestQueueAndProcessMessageQueue:
    def test_queue_feed_message_inserts_row(self, fm_with_db):
        fm = fm_with_db
        with fm.bot.db_manager.connection() as conn:
            conn.execute(
                "INSERT INTO feed_subscriptions (feed_type, feed_url, channel_name, message_send_interval_seconds) VALUES (?,?,?,?)",
                ("rss", "http://x", "#alerts", 0.0),
            )
            conn.commit()
            fid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        feed = {"id": fid, "channel_name": "#alerts"}
        item = {"id": "i1", "title": "T1"}
        fm._queue_feed_message(feed, item, "hello mesh")

        with fm.bot.db_manager.connection() as conn:
            row = conn.execute(
                "SELECT message, sent_at FROM feed_message_queue WHERE feed_id = ?",
                (fid,),
            ).fetchone()
            assert row[0] == "hello mesh"
            assert row[1] is None

    @pytest.mark.asyncio
    async def test_process_message_queue_sends_and_marks_sent(self, fm_with_db):
        fm = fm_with_db
        bot = fm.bot
        bot.command_manager = MagicMock()
        bot.command_manager.send_channel_message = AsyncMock(return_value=True)

        with bot.db_manager.connection() as conn:
            conn.execute(
                "INSERT INTO feed_subscriptions (feed_type, feed_url, channel_name, message_send_interval_seconds) VALUES (?,?,?,?)",
                ("rss", "http://y", "#news", 0.0),
            )
            fid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                """INSERT INTO feed_message_queue (feed_id, channel_name, message, item_id, item_title, priority)
                   VALUES (?,?,?,?,?,0)""",
                (fid, "#news", "queued body", "q1", "Queued title"),
            )
            conn.commit()

        await fm.process_message_queue()

        bot.command_manager.send_channel_message.assert_awaited_once_with("#news", "queued body")

        with bot.db_manager.connection() as conn:
            sent = conn.execute(
                "SELECT sent_at IS NOT NULL FROM feed_message_queue WHERE item_id = ?",
                ("q1",),
            ).fetchone()[0]
            assert sent == 1

            act = conn.execute(
                "SELECT COUNT(*) FROM feed_activity WHERE feed_id = ? AND item_id = ?",
                (fid, "q1"),
            ).fetchone()[0]
            assert act == 1


class TestPollFeedPosting:
    """poll_feed: max_items_per_check (scan window) vs max_posts_per_check (post cap)."""

    def _prep(self, fm, monkeypatch, items):
        fm._url_policy.validate_async = AsyncMock(return_value=True)
        fm._ensure_session = AsyncMock()
        fm._wait_for_rate_limit = AsyncMock()
        fm._update_feed_last_check = Mock()
        fm.process_rss_feed = AsyncMock(return_value=items)
        fm._should_send_item = lambda feed, item: item["pass"]
        sent = AsyncMock()
        fm._send_feed_item = sent
        return sent

    @staticmethod
    def _feed():
        return {
            "id": 1,
            "feed_type": "rss",
            "feed_url": "http://example.com/feed.xml",
            "channel_name": "general",
        }

    @pytest.mark.asyncio
    async def test_posted_cap_scans_past_filtered_items(self, fm_with_db, monkeypatch):
        fm = fm_with_db
        fm.max_items_per_check = 100
        fm.max_posts_per_check = 2
        # 5 items that fail the filter followed by 5 that pass
        items = [{"id": f"f{i}", "title": f"f{i}", "pass": False} for i in range(5)]
        items += [{"id": f"p{i}", "title": f"p{i}", "pass": True} for i in range(5)]
        sent = self._prep(fm, monkeypatch, items)

        await fm.poll_feed(self._feed())

        # Scanned past the filtered items and stopped at the posted cap
        assert sent.call_count == 2
        assert [c.args[1]["title"] for c in sent.call_args_list] == ["p0", "p1"]

    @pytest.mark.asyncio
    async def test_examine_window_bounds_scan(self, fm_with_db, monkeypatch):
        fm = fm_with_db
        fm.max_items_per_check = 3
        fm.max_posts_per_check = 10
        # First 3 fail; passing items sit beyond the 3-item scan window
        items = [{"id": f"f{i}", "title": f"f{i}", "pass": False} for i in range(3)]
        items += [{"id": f"p{i}", "title": f"p{i}", "pass": True} for i in range(3)]
        sent = self._prep(fm, monkeypatch, items)

        await fm.poll_feed(self._feed())

        assert sent.call_count == 0

    @pytest.mark.asyncio
    async def test_default_parity_posts_up_to_ten(self, fm_with_db, monkeypatch):
        fm = fm_with_db
        # Defaults: both 10 -> same behavior as before the max_posts_per_check change
        fm.max_items_per_check = 10
        fm.max_posts_per_check = 10
        items = [{"id": f"p{i}", "title": f"p{i}", "pass": True} for i in range(12)]
        sent = self._prep(fm, monkeypatch, items)

        await fm.poll_feed(self._feed())

        assert sent.call_count == 10


class TestFeedLimitClamping:
    """Nonsensical numeric limits must not reach the slice/loop arithmetic.

    Every one of these is a list slice or an aiohttp timeout where 0 or a
    negative silently does something other than "no limit": a negative slice
    drops the newest items, and ClientTimeout(total=0) arms no timer at all.
    """

    @staticmethod
    def _manager(mock_logger, tmp_path, **overrides):
        bot = _feed_manager_bot(mock_logger, str(tmp_path / "clamp.db"))
        for key, value in overrides.items():
            bot.config.set("Feed_Manager", key, value)
        return FeedManager(bot)

    @pytest.mark.parametrize("raw,expected", [("-5", 1), ("0", 1), ("1", 1), ("7", 7)])
    def test_scan_and_post_windows_clamped(self, mock_logger, tmp_path, raw, expected):
        fm = self._manager(
            mock_logger, tmp_path,
            max_items_per_check=raw, max_posts_per_check=raw,
        )
        assert fm.max_items_per_check == expected
        assert fm.max_posts_per_check == expected

    @pytest.mark.parametrize("raw,expected", [("-1", 1), ("0", 1), ("45", 45)])
    def test_request_timeout_clamped(self, mock_logger, tmp_path, raw, expected):
        """aiohttp only arms a timer for total > 0; 0 means *no* timeout."""
        fm = self._manager(mock_logger, tmp_path, feed_request_timeout=raw)
        assert fm.request_timeout == expected

    @pytest.mark.parametrize("raw,expected", [("0", 10), ("2", 10), ("200", 200)])
    def test_message_length_clamped(self, mock_logger, tmp_path, raw, expected):
        """`message[:max_message_length - 3]` goes negative below 4."""
        fm = self._manager(mock_logger, tmp_path, max_message_length=raw)
        assert fm.max_message_length == expected

    def test_post_budget_of_zero_sends_nothing(self, mock_logger, tmp_path):
        """The cap is checked before the send, so 0 posts none even when set
        directly on the instance (bypassing __init__'s clamp)."""
        fm = self._manager(mock_logger, tmp_path)
        fm.max_posts_per_check = 0
        fm._should_send_item = Mock(return_value=True)
        fm._send_feed_item = AsyncMock()
        fm.process_rss_feed = AsyncMock(return_value=[{"title": f"i{i}"} for i in range(5)])
        fm._update_feed_last_check = Mock()

        feed = {
            "id": 1, "feed_type": "rss", "feed_url": "http://x/f",
            "channel_name": "general",
        }
        asyncio.run(fm.poll_feed(feed))

        assert fm._send_feed_item.await_count == 0
