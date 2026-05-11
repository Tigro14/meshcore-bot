"""Tests for modules.commands.bbs_command."""

import configparser
import sqlite3
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from modules.commands.bbs_command import BBSCommand, _NOTIFICATION_COOLDOWN_SECONDS
from tests.conftest import mock_message

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TRACKED_CONNECTIONS: list[sqlite3.Connection] = []


def _create_tracked_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    _TRACKED_CONNECTIONS.append(conn)
    return conn


@pytest.fixture(autouse=True)
def _close_tracked_connections():
    """Ensure each test closes its SQLite connections."""
    yield
    while _TRACKED_CONNECTIONS:
        conn = _TRACKED_CONNECTIONS.pop()
        try:
            conn.close()
        except sqlite3.Error:
            pass


def _make_db_manager() -> MagicMock:
    """Create a mock db_manager backed by an in-memory SQLite database."""
    conn = _create_tracked_connection()
    # Create the bbs_messages table
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bbs_messages (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id      TEXT NOT NULL,
            sender_name    TEXT,
            recipient_name TEXT NOT NULL,
            message        TEXT NOT NULL,
            sent_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            read_at        TIMESTAMP
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bbs_recipient "
        "ON bbs_messages(recipient_name, read_at)"
    )
    conn.commit()

    db = MagicMock()

    @contextmanager
    def _conn_ctx():
        yield conn

    db.connection = _conn_ctx
    db.db_path = ":memory:"
    return db


def _make_bot(enabled: bool = True, max_messages: int = 5) -> MagicMock:
    """Create a minimal mock bot for BBS command tests."""
    bot = MagicMock()
    bot.logger = Mock()
    config = configparser.ConfigParser()
    config.add_section("Bot")
    config.set("Bot", "bot_name", "TestBot")
    config.add_section("Channels")
    config.set("Channels", "monitor_channels", "general")
    config.set("Channels", "respond_to_dms", "true")
    config.add_section("Keywords")
    config.add_section("BBS_Command")
    config.set("BBS_Command", "enabled", str(enabled).lower())
    config.set("BBS_Command", "max_messages_per_user", str(max_messages))
    config.set("BBS_Command", "max_message_age_days", "7")
    bot.config = config
    bot.translator = MagicMock()
    bot.translator.translate = Mock(side_effect=lambda key, **kw: key)
    bot.translator.get_value = Mock(return_value=None)
    bot.command_manager = MagicMock()
    bot.command_manager.monitor_channels = ["general"]
    bot.command_manager.send_response = AsyncMock(return_value=True)
    bot.db_manager = _make_db_manager()
    return bot


# ---------------------------------------------------------------------------
# Unit tests — _is_bbs_read_command
# ---------------------------------------------------------------------------


class TestIsBBSReadCommand:
    def test_bbs_r(self):
        assert BBSCommand._is_bbs_read_command("bbs r") is True

    def test_bbs_read(self):
        assert BBSCommand._is_bbs_read_command("bbs read") is True

    def test_bbs_l(self):
        assert BBSCommand._is_bbs_read_command("bbs l") is True

    def test_bbs_list(self):
        assert BBSCommand._is_bbs_read_command("bbs list") is True

    def test_bbs_r_with_trailing(self):
        assert BBSCommand._is_bbs_read_command("bbs r extra") is True

    def test_send_is_not_read(self):
        assert BBSCommand._is_bbs_read_command("s John hi") is False

    def test_bbs_s_is_not_read(self):
        assert BBSCommand._is_bbs_read_command("bbs s John hi") is False


# ---------------------------------------------------------------------------
# Unit tests — DB helpers
# ---------------------------------------------------------------------------


class TestBBSDBHelpers:
    """Tests for store / count / fetch / mark-read helpers."""

    def setup_method(self):
        self.cmd = BBSCommand(_make_bot())

    def test_store_and_count(self):
        assert self.cmd._store_message("id1", "Alice", "Bob", "Hello Bob!") is True
        assert self.cmd._get_pending_count("Bob") == 1

    def test_pending_count_case_insensitive(self):
        self.cmd._store_message("id1", "Alice", "Bob", "hi")
        assert self.cmd._get_pending_count("bob") == 1
        assert self.cmd._get_pending_count("BOB") == 1

    def test_count_zero_when_no_messages(self):
        assert self.cmd._get_pending_count("Nobody") == 0

    def test_get_pending_messages_returns_rows(self):
        self.cmd._store_message("id1", "Alice", "Bob", "msg1")
        self.cmd._store_message("id2", "Carol", "Bob", "msg2")
        rows = self.cmd._get_pending_messages("Bob")
        assert len(rows) == 2

    def test_get_pending_messages_oldest_first(self):
        self.cmd._store_message("id1", "Alice", "Bob", "first")
        self.cmd._store_message("id2", "Alice", "Bob", "second")
        rows = self.cmd._get_pending_messages("Bob")
        _id0, from0, text0, _ = rows[0]
        _id1, from1, text1, _ = rows[1]
        assert text0 == "first"
        assert text1 == "second"

    def test_mark_messages_read(self):
        self.cmd._store_message("id1", "Alice", "Bob", "hi")
        count = self.cmd._mark_messages_read("Bob")
        assert count == 1
        assert self.cmd._get_pending_count("Bob") == 0

    def test_mark_messages_read_does_not_affect_other_users(self):
        self.cmd._store_message("id1", "Alice", "Bob", "for Bob")
        self.cmd._store_message("id1", "Alice", "Carol", "for Carol")
        self.cmd._mark_messages_read("Bob")
        assert self.cmd._get_pending_count("Bob") == 0
        assert self.cmd._get_pending_count("Carol") == 1

    def test_mailbox_full_returns_false(self):
        bot = _make_bot(max_messages=2)
        cmd = BBSCommand(bot)
        assert cmd._store_message("id1", "Alice", "Bob", "msg1") is True
        assert cmd._store_message("id1", "Alice", "Bob", "msg2") is True
        assert cmd._store_message("id1", "Alice", "Bob", "msg3 overflow") is False

    def test_already_read_messages_not_counted(self):
        self.cmd._store_message("id1", "Alice", "Bob", "hi")
        self.cmd._mark_messages_read("Bob")
        # Mailbox is empty — can store again
        assert self.cmd._store_message("id1", "Alice", "Bob", "new msg") is True

    def test_get_pending_returns_empty_after_read(self):
        self.cmd._store_message("id1", "Alice", "Bob", "hi")
        self.cmd._mark_messages_read("Bob")
        rows = self.cmd._get_pending_messages("Bob")
        assert rows == []


# ---------------------------------------------------------------------------
# Integration tests — execute()
# ---------------------------------------------------------------------------


class TestBBSExecute:
    """Tests for the execute() method routing."""

    def setup_method(self):
        self.bot = _make_bot()
        self.cmd = BBSCommand(self.bot)
        self.bot.command_manager.send_response = AsyncMock(return_value=True)

    @pytest.mark.asyncio
    async def test_send_shorthand_s(self):
        msg = mock_message("s Alice Hello!", is_dm=True, sender_id="Bob")
        await self.cmd.execute(msg)
        assert self.cmd._get_pending_count("Alice") == 1

    @pytest.mark.asyncio
    async def test_send_shorthand_send(self):
        msg = mock_message("send Alice Hi there", is_dm=True, sender_id="Bob")
        await self.cmd.execute(msg)
        assert self.cmd._get_pending_count("Alice") == 1

    @pytest.mark.asyncio
    async def test_send_bbs_prefix(self):
        msg = mock_message("bbs s Alice Hey", is_dm=True, sender_id="Bob")
        await self.cmd.execute(msg)
        assert self.cmd._get_pending_count("Alice") == 1

    @pytest.mark.asyncio
    async def test_send_bbs_send_prefix(self):
        msg = mock_message("bbs send Alice Hey", is_dm=True, sender_id="Bob")
        await self.cmd.execute(msg)
        assert self.cmd._get_pending_count("Alice") == 1

    @pytest.mark.asyncio
    async def test_send_missing_message_returns_usage(self):
        msg = mock_message("s Alice", is_dm=True, sender_id="Bob")
        await self.cmd.execute(msg)
        # Should show usage, not store a message
        assert self.cmd._get_pending_count("Alice") == 0
        self.bot.command_manager.send_response.assert_called_once()

    @pytest.mark.asyncio
    async def test_read_dm_returns_messages(self):
        self.cmd._store_message("id_alice", "Alice", "Bob", "Hi Bob!")
        msg = mock_message("bbs r", is_dm=True, sender_id="Bob")
        await self.cmd.execute(msg)
        # After reading, messages marked as read
        assert self.cmd._get_pending_count("Bob") == 0

    @pytest.mark.asyncio
    async def test_read_no_messages(self):
        msg = mock_message("bbs r", is_dm=True, sender_id="Bob")
        await self.cmd.execute(msg)
        self.bot.command_manager.send_response.assert_called_once()
        call_args = self.bot.command_manager.send_response.call_args
        assert "No pending" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_read_channel_rejected(self):
        msg = mock_message("bbs r", is_dm=False, sender_id="Bob", channel="general")
        await self.cmd.execute(msg)
        call_args = self.bot.command_manager.send_response.call_args
        assert "only available via DM" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_list_dm_shows_count(self):
        self.cmd._store_message("id_alice", "Alice", "Bob", "msg1")
        self.cmd._store_message("id_carol", "Carol", "Bob", "msg2")
        msg = mock_message("bbs list", is_dm=True, sender_id="Bob")
        await self.cmd.execute(msg)
        call_args = self.bot.command_manager.send_response.call_args
        assert "2" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_list_channel_rejected(self):
        msg = mock_message("bbs l", is_dm=False, sender_id="Bob", channel="general")
        await self.cmd.execute(msg)
        call_args = self.bot.command_manager.send_response.call_args
        assert "only available via DM" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_help_shown_for_bare_bbs(self):
        msg = mock_message("bbs", is_dm=True, sender_id="Bob")
        await self.cmd.execute(msg)
        call_args = self.bot.command_manager.send_response.call_args
        assert "s <name>" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_unknown_subcommand_shows_help(self):
        msg = mock_message("bbs unknowncmd", is_dm=True, sender_id="Bob")
        await self.cmd.execute(msg)
        call_args = self.bot.command_manager.send_response.call_args
        assert "s <name>" in call_args[0][1]


# ---------------------------------------------------------------------------
# Tests — check_inbox_notification
# ---------------------------------------------------------------------------


class TestBBSInboxNotification:
    def setup_method(self):
        self.bot = _make_bot()
        self.cmd = BBSCommand(self.bot)
        self.bot.command_manager.send_response = AsyncMock(return_value=True)

    @pytest.mark.asyncio
    async def test_notification_sent_when_pending(self):
        self.cmd._store_message("id_alice", "Alice", "Bob", "Hi Bob!")
        msg = mock_message("hello", is_dm=True, sender_id="Bob")
        await self.cmd.check_inbox_notification(msg)
        self.bot.command_manager.send_response.assert_called_once()
        text = self.bot.command_manager.send_response.call_args[0][1]
        assert "pending" in text.lower()

    @pytest.mark.asyncio
    async def test_notification_not_sent_when_no_messages(self):
        msg = mock_message("hello", is_dm=True, sender_id="Bob")
        await self.cmd.check_inbox_notification(msg)
        self.bot.command_manager.send_response.assert_not_called()

    @pytest.mark.asyncio
    async def test_notification_skipped_for_channel_message(self):
        self.cmd._store_message("id_alice", "Alice", "Bob", "hi")
        msg = mock_message("hello", is_dm=False, sender_id="Bob", channel="general")
        await self.cmd.check_inbox_notification(msg)
        self.bot.command_manager.send_response.assert_not_called()

    @pytest.mark.asyncio
    async def test_notification_skipped_on_bbs_read(self):
        self.cmd._store_message("id_alice", "Alice", "Bob", "hi")
        msg = mock_message("bbs r", is_dm=True, sender_id="Bob")
        await self.cmd.check_inbox_notification(msg)
        self.bot.command_manager.send_response.assert_not_called()

    @pytest.mark.asyncio
    async def test_notification_cooldown_prevents_repeat(self):
        import time

        self.cmd._store_message("id_alice", "Alice", "Bob", "hi")
        msg = mock_message("hello", is_dm=True, sender_id="Bob")
        await self.cmd.check_inbox_notification(msg)
        assert self.bot.command_manager.send_response.call_count == 1

        # Second call immediately — should be blocked by cooldown
        await self.cmd.check_inbox_notification(msg)
        assert self.bot.command_manager.send_response.call_count == 1

    @pytest.mark.asyncio
    async def test_notification_resent_after_cooldown(self):
        import time

        self.cmd._store_message("id_alice", "Alice", "Bob", "hi")
        msg = mock_message("hello", is_dm=True, sender_id="Bob")
        # Pre-expire the cooldown
        self.cmd._notification_cooldowns["Bob"] = (
            time.time() - _NOTIFICATION_COOLDOWN_SECONDS - 1
        )
        await self.cmd.check_inbox_notification(msg)
        assert self.bot.command_manager.send_response.call_count == 1

    @pytest.mark.asyncio
    async def test_disabled_command_skips_notification(self):
        bot = _make_bot(enabled=False)
        cmd = BBSCommand(bot)
        cmd._store_message("id_alice", "Alice", "Bob", "hi")
        msg = mock_message("hello", is_dm=True, sender_id="Bob")
        await cmd.check_inbox_notification(msg)
        bot.command_manager.send_response.assert_not_called()
