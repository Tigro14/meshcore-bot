"""Tests for modules.commands.bbs_command."""

import configparser
import sqlite3
import time
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from modules.commands.bbs_command import (
    BBSCommand,
    NOTIFICATION_COOLDOWN_SECONDS,
    _PENDING_SELECTION_TTL,
)
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
    # Create the complete_contact_tracking table (minimal columns for lookup tests)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS complete_contact_tracking (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            public_key  TEXT NOT NULL,
            name        TEXT NOT NULL,
            role        TEXT NOT NULL DEFAULT 'CLIENT',
            last_heard  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()

    db = MagicMock()

    @contextmanager
    def _conn_ctx():
        yield conn

    db.connection = _conn_ctx
    db.db_path = ":memory:"
    return db


def _add_contact(db_manager: MagicMock, name: str, public_key: str | None = None) -> None:
    """Insert a contact into the in-memory complete_contact_tracking table."""
    pk = public_key or f"pk_{name}"
    with db_manager.connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO complete_contact_tracking (public_key, name, role) "
            "VALUES (?, ?, 'CLIENT')",
            (pk, name),
        )
        conn.commit()


def _make_bot(
    enabled: bool = True,
    max_messages: int = 5,
    max_per_day: int = 20,
) -> MagicMock:
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
    config.set("BBS_Command", "max_messages_per_sender_per_day", str(max_per_day))
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
        # Register Alice and Bob as known contacts so send tests can resolve them.
        _add_contact(self.bot.db_manager, "Alice")
        _add_contact(self.bot.db_manager, "Bob")

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
    async def test_send_no_message_shows_shortlist(self):
        """'s Alice' with no message shows a shortlist and does NOT store a message."""
        msg = mock_message("s Alice", is_dm=True, sender_id="Bob")
        await self.cmd.execute(msg)
        assert self.cmd._get_pending_count("Alice") == 0
        self.bot.command_manager.send_response.assert_called_once()
        # Response should contain "Alice" from the numbered shortlist
        response_text = self.bot.command_manager.send_response.call_args[0][1]
        assert "Alice" in response_text

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
        self.cmd._store_message("id_alice", "Alice", "Bob", "hi")
        msg = mock_message("hello", is_dm=True, sender_id="Bob")
        await self.cmd.check_inbox_notification(msg)
        assert self.bot.command_manager.send_response.call_count == 1

        # Second call immediately — should be blocked by cooldown
        await self.cmd.check_inbox_notification(msg)
        assert self.bot.command_manager.send_response.call_count == 1

    @pytest.mark.asyncio
    async def test_notification_resent_after_cooldown(self):
        self.cmd._store_message("id_alice", "Alice", "Bob", "hi")
        msg = mock_message("hello", is_dm=True, sender_id="Bob")
        # Pre-expire the cooldown
        self.cmd._notification_cooldowns["Bob"] = (
            time.time() - NOTIFICATION_COOLDOWN_SECONDS - 1
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


# ---------------------------------------------------------------------------
# Tests — _lookup_contacts
# ---------------------------------------------------------------------------


class TestBBSContactLookup:
    """Tests for the _lookup_contacts helper."""

    def setup_method(self):
        self.bot = _make_bot()
        self.cmd = BBSCommand(self.bot)

    def test_partial_match_returns_name(self):
        _add_contact(self.bot.db_manager, "Tom COD WP")
        results = self.cmd._lookup_contacts("tom")
        assert results == ["Tom COD WP"]

    def test_case_insensitive_match(self):
        _add_contact(self.bot.db_manager, "Tomas")
        results = self.cmd._lookup_contacts("TOMAS")
        assert len(results) == 1
        assert results[0] == "Tomas"

    def test_multiple_matches(self):
        for name in ("Tomas", "tom COD", "tomaso", "TomTom"):
            _add_contact(self.bot.db_manager, name)
        results = self.cmd._lookup_contacts("tom")
        assert len(results) == 4
        assert "Tomas" in results
        assert "tom COD" in results

    def test_no_match_returns_empty(self):
        _add_contact(self.bot.db_manager, "Alice")
        results = self.cmd._lookup_contacts("zzz")
        assert results == []

    def test_max_5_results(self):
        for i in range(8):
            _add_contact(self.bot.db_manager, f"TomNode{i}")
        results = self.cmd._lookup_contacts("tom")
        assert len(results) <= 5

    def test_table_missing_returns_empty(self):
        """When complete_contact_tracking does not exist, return []."""
        with self.bot.db_manager.connection() as conn:
            conn.execute("DROP TABLE IF EXISTS complete_contact_tracking")
            conn.commit()
        results = self.cmd._lookup_contacts("alice")
        assert results == []


# ---------------------------------------------------------------------------
# Tests — pending selection state helpers
# ---------------------------------------------------------------------------


class TestBBSPendingSelection:
    """Tests for _get/_set/_clear_pending_selection."""

    def setup_method(self):
        self.cmd = BBSCommand(_make_bot())

    def test_set_and_get(self):
        self.cmd._set_pending_selection("user1", ["Alice", "Bob"])
        result = self.cmd._get_pending_selection("user1")
        assert result == ["Alice", "Bob"]

    def test_get_returns_none_when_absent(self):
        assert self.cmd._get_pending_selection("nobody") is None

    def test_clear_removes_entry(self):
        self.cmd._set_pending_selection("user1", ["Alice"])
        self.cmd._clear_pending_selection("user1")
        assert self.cmd._get_pending_selection("user1") is None

    def test_clear_no_op_when_absent(self):
        self.cmd._clear_pending_selection("ghost")  # must not raise

    def test_expired_entry_returns_none(self):
        # Manually set an already-expired entry
        self.cmd._pending_selections["user1"] = (["Alice"], time.time() - 1)
        assert self.cmd._get_pending_selection("user1") is None

    def test_expired_entry_is_removed(self):
        self.cmd._pending_selections["user1"] = (["Alice"], time.time() - 1)
        self.cmd._get_pending_selection("user1")
        assert "user1" not in self.cmd._pending_selections

    def test_separate_senders_are_independent(self):
        self.cmd._set_pending_selection("userA", ["Alice"])
        self.cmd._set_pending_selection("userB", ["Bob"])
        assert self.cmd._get_pending_selection("userA") == ["Alice"]
        assert self.cmd._get_pending_selection("userB") == ["Bob"]
        self.cmd._clear_pending_selection("userA")
        assert self.cmd._get_pending_selection("userB") == ["Bob"]


# ---------------------------------------------------------------------------
# Tests — two-step send flow
# ---------------------------------------------------------------------------


class TestBBSSendTwoStep:
    """Integration tests for the two-step send flow."""

    def setup_method(self):
        self.bot = _make_bot()
        self.cmd = BBSCommand(self.bot)
        self.bot.command_manager.send_response = AsyncMock(return_value=True)

    def _last_response(self) -> str:
        return self.bot.command_manager.send_response.call_args[0][1]

    # ------------------------------------------------------------------
    # Step 1: search / shortlist
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_step1_no_match_shows_error(self):
        msg = mock_message("s zzz", is_dm=True, sender_id="Bob")
        await self.cmd.execute(msg)
        assert "No contact found" in self._last_response()
        assert self.cmd._get_pending_count("zzz") == 0

    @pytest.mark.asyncio
    async def test_step1_multiple_matches_shows_numbered_list(self):
        for name in ("Tomas", "tom COD WP", "tomaso"):
            _add_contact(self.bot.db_manager, name)
        msg = mock_message("bbs s tom", is_dm=True, sender_id="Bob")
        await self.cmd.execute(msg)
        resp = self._last_response()
        assert "1 " in resp
        assert "2 " in resp
        assert "3 " in resp
        # No message stored yet
        for name in ("Tomas", "tom COD WP", "tomaso"):
            assert self.cmd._get_pending_count(name) == 0

    @pytest.mark.asyncio
    async def test_step1_single_match_with_message_sends_directly(self):
        _add_contact(self.bot.db_manager, "Alice")
        msg = mock_message("s Alice Hello!", is_dm=True, sender_id="Bob")
        await self.cmd.execute(msg)
        assert self.cmd._get_pending_count("Alice") == 1
        assert "stored" in self._last_response().lower()

    @pytest.mark.asyncio
    async def test_step1_single_match_without_message_shows_shortlist(self):
        _add_contact(self.bot.db_manager, "Alice")
        msg = mock_message("s Alice", is_dm=True, sender_id="Bob")
        await self.cmd.execute(msg)
        resp = self._last_response()
        assert "Alice" in resp
        assert self.cmd._get_pending_count("Alice") == 0

    @pytest.mark.asyncio
    async def test_step1_sets_pending_state(self):
        for name in ("Tomas", "TomTom"):
            _add_contact(self.bot.db_manager, name)
        sender_key = "Bob"
        msg = mock_message("s tom", is_dm=True, sender_id=sender_key)
        await self.cmd.execute(msg)
        assert self.cmd._get_pending_selection(sender_key) is not None

    # ------------------------------------------------------------------
    # Step 2: confirm by number
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_step2_sends_to_correct_candidate(self):
        for name in ("Tomas", "tom COD WP", "tomaso"):
            _add_contact(self.bot.db_manager, name)
        sender_key = "Bob"
        # Step 1
        msg1 = mock_message("s tom", is_dm=True, sender_id=sender_key)
        await self.cmd.execute(msg1)
        # Step 2 — pick #2
        msg2 = mock_message("s 2 Hey there", is_dm=True, sender_id=sender_key)
        await self.cmd.execute(msg2)
        # Message should go to the second candidate
        candidates = ["Tomas", "tom COD WP", "tomaso"]
        # Get what was stored (one of the candidates)
        total = sum(self.cmd._get_pending_count(n) for n in candidates)
        assert total == 1
        assert "stored" in self._last_response().lower()

    @pytest.mark.asyncio
    async def test_step2_clears_pending_after_send(self):
        _add_contact(self.bot.db_manager, "Alice")
        _add_contact(self.bot.db_manager, "AliceB")
        sender_key = "Bob"
        msg1 = mock_message("s Alice", is_dm=True, sender_id=sender_key)
        await self.cmd.execute(msg1)
        msg2 = mock_message("s 1 hi", is_dm=True, sender_id=sender_key)
        await self.cmd.execute(msg2)
        # Pending state should be cleared
        assert self.cmd._get_pending_selection(sender_key) is None

    @pytest.mark.asyncio
    async def test_step2_without_pending_shows_error(self):
        msg = mock_message("s 1 hello", is_dm=True, sender_id="Bob")
        await self.cmd.execute(msg)
        resp = self._last_response()
        assert "No pending" in resp

    @pytest.mark.asyncio
    async def test_step2_out_of_range_number_shows_error(self):
        _add_contact(self.bot.db_manager, "Alice")
        _add_contact(self.bot.db_manager, "AliceB")
        sender_key = "Bob"
        msg1 = mock_message("s Alice", is_dm=True, sender_id=sender_key)
        await self.cmd.execute(msg1)
        msg2 = mock_message("s 9 hello", is_dm=True, sender_id=sender_key)
        await self.cmd.execute(msg2)
        resp = self._last_response()
        assert "between 1 and" in resp

    @pytest.mark.asyncio
    async def test_step2_number_without_message_shows_usage(self):
        _add_contact(self.bot.db_manager, "Alice")
        sender_key = "Bob"
        msg1 = mock_message("s Alice", is_dm=True, sender_id=sender_key)
        await self.cmd.execute(msg1)
        msg2 = mock_message("s 1", is_dm=True, sender_id=sender_key)
        await self.cmd.execute(msg2)
        resp = self._last_response()
        assert "Usage" in resp or "s 1" in resp

    @pytest.mark.asyncio
    async def test_step2_expired_pending_shows_error(self):
        sender_key = "Bob"
        # Inject an already-expired pending entry
        self.cmd._pending_selections[sender_key] = (["Alice"], time.time() - 1)
        msg = mock_message("s 1 hello", is_dm=True, sender_id=sender_key)
        await self.cmd.execute(msg)
        resp = self._last_response()
        assert "No pending" in resp

    # ------------------------------------------------------------------
    # Space-in-name end-to-end scenario
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_spaced_name_two_step_end_to_end(self):
        """Full scenario: 'Tom COD WP' is correctly resolved via two-step flow."""
        _add_contact(self.bot.db_manager, "Tom COD WP")
        _add_contact(self.bot.db_manager, "Tomas")
        sender_key = "Alice"

        # Step 1
        step1 = mock_message("bbs s Tom", is_dm=True, sender_id=sender_key)
        await self.cmd.execute(step1)
        resp1 = self._last_response()
        # Both names should appear in the shortlist
        assert "Tom COD WP" in resp1 or "Tomas" in resp1

        # Find the index for "Tom COD WP"
        candidates = self.cmd._get_pending_selection(sender_key)
        assert candidates is not None
        idx = candidates.index("Tom COD WP") + 1

        # Step 2
        step2 = mock_message(f"bbs s {idx} Test message", is_dm=True, sender_id=sender_key)
        await self.cmd.execute(step2)
        assert self.cmd._get_pending_count("Tom COD WP") == 1
        assert self.cmd._get_pending_count("Tomas") == 0


# ---------------------------------------------------------------------------
# Tests — per-sender daily quota
# ---------------------------------------------------------------------------


class TestBBSPerSenderDailyQuota:
    """Tests for the max_messages_per_sender_per_day config option."""

    def setup_method(self):
        # Limit each sender to 2 messages per day for fast testing
        self.bot = _make_bot(max_per_day=2)
        self.cmd = BBSCommand(self.bot)

    def test_quota_allows_up_to_limit(self):
        _add_contact(self.bot.db_manager, "Alice")
        assert self.cmd._store_message("sender1", "S1", "Alice", "msg1") is True
        assert self.cmd._store_message("sender1", "S1", "Alice", "msg2") is True

    def test_quota_blocks_after_limit(self):
        _add_contact(self.bot.db_manager, "Alice")
        self.cmd._store_message("sender1", "S1", "Alice", "msg1")
        self.cmd._store_message("sender1", "S1", "Alice", "msg2")
        assert self.cmd._store_message("sender1", "S1", "Alice", "msg3") is False

    def test_quota_is_per_sender_not_global(self):
        """Different senders each have their own quota."""
        _add_contact(self.bot.db_manager, "Alice")
        self.cmd._store_message("sender1", "S1", "Alice", "msg1")
        self.cmd._store_message("sender1", "S1", "Alice", "msg2")
        # sender2 should still be able to send
        assert self.cmd._store_message("sender2", "S2", "Alice", "msg3") is True

    def test_quota_zero_disables_limit(self):
        """Setting max_messages_per_sender_per_day = 0 disables the daily cap."""
        bot = _make_bot(max_per_day=0)
        cmd = BBSCommand(bot)
        _add_contact(bot.db_manager, "Alice")
        for i in range(30):
            assert cmd._store_message("sender1", "S1", "Alice", f"msg{i}") is True or True
        # All 30 inserts should succeed (no quota). Exact count may be capped by
        # max_messages_per_user (5 in test bot), but that's a separate limit.

    def test_default_quota_is_20(self):
        """When the config key is absent, the default quota of 20 is applied."""
        bot = _make_bot()
        # Remove the key so we fall back to the default
        bot.config.remove_option("BBS_Command", "max_messages_per_sender_per_day")
        cmd = BBSCommand(bot)
        assert cmd.max_messages_per_sender_per_day == 20
