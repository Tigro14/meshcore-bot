"""Tests for modules.commands.bbs_command."""

import configparser
import sqlite3
import time
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from modules.commands.bbs_command import (
    DEFAULT_FINAL_NOTICE_HOURS,
    DEFAULT_REMINDER_HOURS,
    NOTIFICATION_COOLDOWN_SECONDS,
    BBSCommand,
)
from modules.db_migrations import (
    _m0035_bbs_inbox_table,
    _m0036_bbs_inbox_notification_stages,
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


def _make_db_manager(
    with_bbs_inbox: bool = True,
    with_legacy: bool = True,
    with_contacts: bool = True,
) -> MagicMock:
    """Create a mock db_manager backed by an in-memory SQLite database."""
    conn = _create_tracked_connection()
    if with_bbs_inbox:
        # Use the real migrations so the test schema can never drift from prod.
        _m0035_bbs_inbox_table(conn.cursor())
        _m0036_bbs_inbox_notification_stages(conn.cursor())
    if with_legacy:
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
    if with_contacts:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS complete_contact_tracking (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                public_key  TEXT NOT NULL,
                name        TEXT NOT NULL,
                role        TEXT NOT NULL DEFAULT 'companion',
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


def _add_contact(
    db_manager: MagicMock,
    name: str,
    public_key: str | None = None,
    role: str = "companion",
) -> None:
    """Insert a contact into the in-memory complete_contact_tracking table."""
    pk = public_key or f"pk_{name}"
    with db_manager.connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO complete_contact_tracking (public_key, name, role) "
            "VALUES (?, ?, ?)",
            (pk, name, role),
        )
        conn.commit()


def _make_bot(
    enabled: bool = True,
    max_messages: int = 5,
    max_per_day: int = 20,
    rate_limit: int = 10,
    notify_on_arrival: bool = True,
    reminder_hours: int = DEFAULT_REMINDER_HOURS,
    final_hours: int = DEFAULT_FINAL_NOTICE_HOURS,
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
    config.set("BBS_Command", "rate_limit_per_minute", str(rate_limit))
    config.set("BBS_Command", "notify_on_arrival", str(notify_on_arrival).lower())
    config.set("BBS_Command", "notify_reminder_hours", str(reminder_hours))
    config.set("BBS_Command", "notify_final_hours", str(final_hours))
    bot.config = config
    bot.translator = MagicMock()
    bot.translator.translate = Mock(side_effect=lambda key, **kw: key)
    bot.translator.get_value = Mock(return_value=None)
    bot.command_manager = MagicMock()
    bot.command_manager.monitor_channels = ["general"]
    bot.command_manager.send_response = AsyncMock(return_value=True)
    # Proactive recipient DMs (arrival / 24h / 73h stages) go through send_dm.
    bot.command_manager.send_dm = AsyncMock(return_value=True)
    bot.db_manager = _make_db_manager()
    return bot


def _last_response(bot: MagicMock) -> str:
    """Text of the last send_response call."""
    return bot.command_manager.send_response.call_args[0][1]


def _all_responses(bot: MagicMock) -> list[str]:
    return [call[0][1] for call in bot.command_manager.send_response.call_args_list]


def _old_ts(days: int) -> str:
    """UTC timestamp *days* ago, matching SQLite's CURRENT_TIMESTAMP format."""
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def _insert_legacy_message(
    db_manager: MagicMock,
    sender_name: str,
    recipient_name: str,
    text: str,
    sent_at: str | None = None,
) -> None:
    with db_manager.connection() as conn:
        conn.execute(
            "INSERT INTO bbs_messages (sender_id, sender_name, recipient_name, message, sent_at) "
            "VALUES (?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))",
            (f"id_{sender_name}", sender_name, recipient_name, text, sent_at),
        )
        conn.commit()


def _insert_inbox_message(
    db_manager: MagicMock,
    sender_key: str,
    recipient_key: str,
    text: str,
    sender_name: str = "Alice",
    recipient_name: str = "Bob",
    sent_at: str | None = None,
) -> None:
    with db_manager.connection() as conn:
        conn.execute(
            "INSERT INTO bbs_inbox "
            "(sender_key, sender_name, recipient_key, recipient_name, message, sent_at) "
            "VALUES (?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))",
            (sender_key, sender_name, recipient_key, recipient_name, text, sent_at),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Unit tests — BBS command-message guard
# ---------------------------------------------------------------------------


class TestIsBBSCommandMessage:
    def test_bare_bbs(self):
        assert BBSCommand._is_bbs_command_message("bbs") is True

    def test_bbs_read(self):
        assert BBSCommand._is_bbs_command_message("bbs r") is True

    def test_shorthand_s(self):
        assert BBSCommand._is_bbs_command_message("s John hi") is True

    def test_shorthand_send(self):
        assert BBSCommand._is_bbs_command_message("send John hi") is True

    def test_plain_message_is_not_bbs(self):
        assert BBSCommand._is_bbs_command_message("hello there") is False

    def test_word_starting_with_s_is_not_bbs(self):
        assert BBSCommand._is_bbs_command_message("sudo make coffee") is False

    def test_empty(self):
        assert BBSCommand._is_bbs_command_message("") is False


# ---------------------------------------------------------------------------
# Unit tests — contact resolution (exact name → key prefix → partial name)
# ---------------------------------------------------------------------------


class TestBBSContactResolution:
    def setup_method(self):
        self.bot = _make_bot()
        self.cmd = BBSCommand(self.bot)

    def test_exact_name_match(self):
        _add_contact(self.bot.db_manager, "Tom COD WP", public_key="aabbccdd11")
        results = self.cmd._resolve_recipient("Tom COD WP")
        assert results == [("Tom COD WP", "aabbccdd11")]

    def test_exact_name_case_insensitive(self):
        _add_contact(self.bot.db_manager, "Tomas", public_key="aabb11")
        results = self.cmd._resolve_recipient("tomas")
        assert len(results) == 1
        assert results[0][0] == "Tomas"

    def test_partial_name_match(self):
        _add_contact(self.bot.db_manager, "Tom Jones", public_key="aabb22")
        results = self.cmd._resolve_recipient("jone")
        assert results == [("Tom Jones", "aabb22")]

    def test_key_prefix_lookup(self):
        _add_contact(self.bot.db_manager, "Alice", public_key="a1b2c3d4e5f6")
        results = self.cmd._resolve_recipient("a1b2")
        assert results == [("Alice", "a1b2c3d4e5f6")]

    def test_exact_name_wins_over_key_prefix(self):
        # A contact literally named "abcdef" takes priority over key-prefix
        # interpretation of the same token.
        _add_contact(self.bot.db_manager, "abcdef", public_key="ffee0000")
        _add_contact(self.bot.db_manager, "Other", public_key="abcdef9999")
        results = self.cmd._resolve_recipient("abcdef")
        assert results == [("abcdef", "ffee0000")]

    def test_non_hex_token_not_resolved_as_key(self):
        # "xyz1" is not hexadecimal → never matched against public keys.
        _add_contact(self.bot.db_manager, "Zoid", public_key="xyz10000")
        results = self.cmd._resolve_recipient("xyz1")
        assert results == []

    def test_hex_token_falls_back_to_name_when_no_key_match(self):
        _add_contact(self.bot.db_manager, "Beefface", public_key="11223344")
        results = self.cmd._resolve_recipient("beef")
        # "beef" is hex but no key starts with it → partial name match hits.
        assert results == [("Beefface", "11223344")]

    def test_no_match_returns_empty(self):
        _add_contact(self.bot.db_manager, "Alice")
        assert self.cmd._resolve_recipient("zzz") == []

    def test_shortlist_max_5_and_deduplicated_by_key(self):
        for i in range(8):
            _add_contact(self.bot.db_manager, f"Tom {i}", public_key=f"aa{i:02d}bb")
        # Duplicate of Tom 3 under the same key must not add an entry.
        _add_contact(self.bot.db_manager, "Tom duplicate", public_key="aa03bb")
        results = self.cmd._lookup_contacts("Tom", match="partial")
        assert len(results) == 5
        keys = [key for _name, key in results]
        assert len(set(keys)) == 5

    def test_non_companion_roles_excluded(self):
        _add_contact(self.bot.db_manager, "Router Tom", public_key="aa01", role="router")
        assert self.cmd._resolve_recipient("Router Tom") == []

    def test_missing_contacts_table_returns_empty(self):
        bot = _make_bot()
        bot.db_manager = _make_db_manager(with_contacts=False)
        cmd = BBSCommand(bot)
        assert cmd._resolve_recipient("Alice") == []

    def test_empty_query(self):
        assert self.cmd._resolve_recipient("") == []

    def test_format_candidate_shows_key_prefix(self):
        assert BBSCommand._format_candidate("John", "a1b2c3d4") == "John [a1b2]"
        assert BBSCommand._format_candidate("John", "") == "John"


# ---------------------------------------------------------------------------
# Unit tests — bbs_inbox DB helpers
# ---------------------------------------------------------------------------


class TestBBSInboxDBHelpers:
    """Tests for store / count / fetch / mark-read helpers on bbs_inbox."""

    def setup_method(self):
        self.cmd = BBSCommand(_make_bot())

    def test_store_and_count(self):
        assert self.cmd._store_message("key_alice", "Alice", "key_bob", "Bob", "Hello Bob!") == "ok"
        assert self.cmd._get_pending_count("key_bob", "Bob") == 1

    def test_count_zero_when_no_messages(self):
        assert self.cmd._get_pending_count("key_nobody", "Nobody") == 0

    def test_get_pending_messages_returns_rows(self):
        self.cmd._store_message("key_alice", "Alice", "key_bob", "Bob", "msg1")
        self.cmd._store_message("key_carol", "Carol", "key_bob", "Bob", "msg2")
        rows = self.cmd._get_pending_messages_by_key("key_bob")
        assert len(rows) == 2

    def test_get_pending_messages_oldest_first(self):
        self.cmd._store_message("key_alice", "Alice", "key_bob", "Bob", "first")
        self.cmd._store_message("key_alice", "Alice", "key_bob", "Bob", "second")
        rows = self.cmd._get_pending_messages_by_key("key_bob")
        assert rows[0][2] == "first"
        assert rows[1][2] == "second"

    def test_same_name_different_keys_are_separate_mailboxes(self):
        self.cmd._store_message("k1", "Alice", "key_bob_1", "Bob", "for Bob #1")
        self.cmd._store_message("k1", "Alice", "key_bob_2", "Bob", "for Bob #2")
        assert len(self.cmd._get_pending_messages_by_key("key_bob_1")) == 1
        assert len(self.cmd._get_pending_messages_by_key("key_bob_2")) == 1

    def test_key_lookup_is_exact_not_case_loose(self):
        # Public keys are hex-lowercase; count by key is exact-match SQL.
        self.cmd._store_message("k1", "Alice", "abcd", "Bob", "hi")
        assert self.cmd._get_pending_count("abcd", "Bob") == 1
        assert self.cmd._get_pending_count("ABCD", "Bob") == 0

    def test_mark_messages_read(self):
        self.cmd._store_message("key_alice", "Alice", "key_bob", "Bob", "hi")
        count = self.cmd._mark_messages_read("key_bob", "Bob")
        assert count == 1
        assert self.cmd._get_pending_count("key_bob", "Bob") == 0

    def test_mark_messages_read_does_not_affect_other_users(self):
        self.cmd._store_message("k1", "Alice", "key_bob", "Bob", "for Bob")
        self.cmd._store_message("k1", "Alice", "key_carol", "Carol", "for Carol")
        self.cmd._mark_messages_read("key_bob", "Bob")
        assert self.cmd._get_pending_count("key_bob", "Bob") == 0
        assert self.cmd._get_pending_count("key_carol", "Carol") == 1

    def test_mailbox_full(self):
        bot = _make_bot(max_messages=2)
        cmd = BBSCommand(bot)
        assert cmd._store_message("k1", "Alice", "key_bob", "Bob", "msg1") == "ok"
        assert cmd._store_message("k1", "Alice", "key_bob", "Bob", "msg2") == "ok"
        assert cmd._store_message("k1", "Alice", "key_bob", "Bob", "msg3 overflow") == "full"

    def test_mailbox_unlimited_when_zero(self):
        bot = _make_bot(max_messages=0)
        cmd = BBSCommand(bot)
        for i in range(3):
            assert cmd._store_message("k1", "Alice", "key_bob", "Bob", f"msg{i}") == "ok"

    def test_already_read_messages_not_counted(self):
        self.cmd._store_message("key_alice", "Alice", "key_bob", "Bob", "hi")
        self.cmd._mark_messages_read("key_bob", "Bob")
        # Mailbox is empty — can store again
        assert self.cmd._store_message("key_alice", "Alice", "key_bob", "Bob", "new msg") == "ok"

    def test_get_pending_returns_empty_after_read(self):
        self.cmd._store_message("key_alice", "Alice", "key_bob", "Bob", "hi")
        self.cmd._mark_messages_read("key_bob", "Bob")
        assert self.cmd._get_pending_messages_by_key("key_bob") == []

    def test_store_error_when_table_missing(self):
        bot = _make_bot()
        bot.db_manager = _make_db_manager(with_bbs_inbox=False)
        cmd = BBSCommand(bot)
        assert cmd._store_message("k1", "Alice", "k2", "Bob", "hi") == "error"

    def test_format_inbox_line_includes_text(self):
        line = BBSCommand._format_inbox_line("Alice", "2026-11-06 10:30:00", "Meet at noon.")
        assert "From Alice [" in line
        assert "11/06 10:30" in line
        assert "Meet at noon." in line

    def test_format_inbox_line_bad_timestamp(self):
        line = BBSCommand._format_inbox_line("Alice", "not-a-date", "hi")
        assert line == "From Alice [not-a-date]: hi"


# ---------------------------------------------------------------------------
# Unit tests — per-sender daily quota (keyed on sender key)
# ---------------------------------------------------------------------------


class TestBBSDailyQuota:
    def setup_method(self):
        self.bot = _make_bot(max_per_day=2)
        self.cmd = BBSCommand(self.bot)

    def test_quota_allows_up_to_limit(self):
        assert self.cmd._store_message("k1", "Alice", "k2", "Bob", "m1") == "ok"
        assert self.cmd._store_message("k1", "Alice", "k3", "Carol", "m2") == "ok"

    def test_quota_blocks_after_limit(self):
        self.cmd._store_message("k1", "Alice", "k2", "Bob", "m1")
        self.cmd._store_message("k1", "Alice", "k3", "Carol", "m2")
        assert self.cmd._store_message("k1", "Alice", "k4", "Dave", "m3") == "quota"

    def test_quota_is_per_sender_not_global(self):
        self.cmd._store_message("k1", "Alice", "k2", "Bob", "m1")
        self.cmd._store_message("k1", "Alice", "k3", "Carol", "m2")
        assert self.cmd._store_message("k9", "Zoe", "k4", "Dave", "m3") == "ok"

    def test_quota_zero_disables_limit(self):
        bot = _make_bot(max_per_day=0)
        cmd = BBSCommand(bot)
        for i in range(5):
            assert cmd._store_message("k1", "Alice", f"k{i}", "Recipient", f"m{i}") == "ok"

    def test_default_quota_is_20(self):
        plain = BBSCommand(_make_bot())
        assert plain.max_messages_per_sender_per_day == 20


# ---------------------------------------------------------------------------
# Unit tests — sliding rate limit
# ---------------------------------------------------------------------------


class TestBBSRateLimit:
    def test_allows_up_to_limit(self):
        cmd = BBSCommand(_make_bot(rate_limit=3))
        assert all(cmd._rate_limit_ok("k1") for _ in range(3))
        assert cmd._rate_limit_ok("k1") is False

    def test_per_key_isolation(self):
        cmd = BBSCommand(_make_bot(rate_limit=1))
        assert cmd._rate_limit_ok("k1") is True
        assert cmd._rate_limit_ok("k1") is False
        assert cmd._rate_limit_ok("k2") is True

    def test_zero_disables_limit(self):
        cmd = BBSCommand(_make_bot(rate_limit=0))
        assert all(cmd._rate_limit_ok("k1") for _ in range(50))

    def test_default_limit_is_10(self):
        cmd = BBSCommand(_make_bot())
        assert cmd.rate_limit_per_minute == 10


# ---------------------------------------------------------------------------
# Unit tests — legacy (name-keyed bbs_messages) helpers
# ---------------------------------------------------------------------------


class TestBSBLegacyHelpers:
    def setup_method(self):
        self.bot = _make_bot()
        self.cmd = BBSCommand(self.bot)

    def test_legacy_count_and_fetch(self):
        _insert_legacy_message(self.bot.db_manager, "OldAlice", "Bob", "legacy msg")
        assert self.cmd._get_legacy_pending_count("Bob") == 1
        rows = self.cmd._get_legacy_pending_messages("bob")
        assert len(rows) == 1
        assert rows[0][2] == "legacy msg"

    def test_legacy_name_match_case_insensitive(self):
        _insert_legacy_message(self.bot.db_manager, "OldAlice", "BoB", "hi")
        assert self.cmd._get_legacy_pending_count("bob") == 1

    def test_combined_count_includes_legacy(self):
        _insert_legacy_message(self.bot.db_manager, "OldAlice", "Bob", "legacy")
        self.cmd._store_message("k1", "Alice", "key_bob", "Bob", "new")
        assert self.cmd._get_pending_count("key_bob", "Bob") == 2

    def test_mark_read_covers_both_tables(self):
        _insert_legacy_message(self.bot.db_manager, "OldAlice", "Bob", "legacy")
        self.cmd._store_message("k1", "Alice", "key_bob", "Bob", "new")
        updated = self.cmd._mark_messages_read("key_bob", "Bob")
        assert updated == 2
        assert self.cmd._get_pending_count("key_bob", "Bob") == 0

    def test_missing_legacy_table_is_tolerated(self):
        bot = _make_bot()
        bot.db_manager = _make_db_manager(with_legacy=False)
        cmd = BBSCommand(bot)
        assert cmd._get_legacy_pending_count("Bob") == 0
        assert cmd._get_legacy_pending_messages("Bob") == []
        assert cmd._get_pending_count("key_bob", "Bob") == 0


# ---------------------------------------------------------------------------
# Unit tests — TTL purge
# ---------------------------------------------------------------------------


class TestBBSPurge:
    def setup_method(self):
        self.bot = _make_bot()
        self.cmd = BBSCommand(self.bot)

    def test_purge_removes_old_messages_from_both_tables(self):
        old = _old_ts(30)
        _insert_inbox_message(self.bot.db_manager, "k1", "k2", "ancient", sent_at=old)
        _insert_legacy_message(self.bot.db_manager, "Alice", "Bob", "ancient legacy", sent_at=old)
        _insert_inbox_message(self.bot.db_manager, "k1", "k2", "fresh")
        self.cmd._purge_old_messages()
        with self.bot.db_manager.connection() as conn:
            inbox_count = conn.execute("SELECT COUNT(*) FROM bbs_inbox").fetchone()[0]
            legacy_count = conn.execute("SELECT COUNT(*) FROM bbs_messages").fetchone()[0]
        assert inbox_count == 1
        assert legacy_count == 0


# ---------------------------------------------------------------------------
# Unit tests — pending recipient selection state
# ---------------------------------------------------------------------------


class TestBBSPendingSelection:
    def setup_method(self):
        self.cmd = BBSCommand(_make_bot())

    def test_set_and_get(self):
        self.cmd._set_pending_selection("k1", [("A", "ka"), ("B", "kb")])
        assert self.cmd._get_pending_selection("k1") == [("A", "ka"), ("B", "kb")]

    def test_get_returns_none_when_absent(self):
        assert self.cmd._get_pending_selection("nobody") is None

    def test_clear_removes_entry(self):
        self.cmd._set_pending_selection("k1", [("A", "ka")])
        self.cmd._clear_pending_selection("k1")
        assert self.cmd._get_pending_selection("k1") is None

    def test_clear_no_op_when_absent(self):
        self.cmd._clear_pending_selection("nobody")  # must not raise

    def test_expired_entry_returns_none(self):
        self.cmd._pending_selections["k1"] = ([("A", "ka")], time.time() - 1)
        assert self.cmd._get_pending_selection("k1") is None
        assert "k1" not in self.cmd._pending_selections

    def test_separate_senders_are_independent(self):
        self.cmd._set_pending_selection("k1", [("A", "ka")])
        self.cmd._set_pending_selection("k2", [("B", "kb")])
        self.cmd._clear_pending_selection("k1")
        assert self.cmd._get_pending_selection("k1") is None
        assert self.cmd._get_pending_selection("k2") == [("B", "kb")]


# ---------------------------------------------------------------------------
# Integration tests — execute() send flow
# ---------------------------------------------------------------------------


class TestBBSSendTwoStep:
    def setup_method(self):
        self.bot = _make_bot()
        self.cmd = BBSCommand(self.bot)
        self.bot.command_manager.send_response = AsyncMock(return_value=True)
        _add_contact(self.bot.db_manager, "Alice", public_key="a1a1a1a1")
        _add_contact(self.bot.db_manager, "Bob", public_key="b2b2b2b2")

    @pytest.mark.asyncio
    async def test_send_shorthand_s(self):
        msg = mock_message("s Alice Hello!", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        await self.cmd.execute(msg)
        assert self.cmd._get_pending_count("a1a1a1a1", "Alice") == 1

    @pytest.mark.asyncio
    async def test_send_shorthand_send(self):
        msg = mock_message("send Alice Hi there", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        await self.cmd.execute(msg)
        assert self.cmd._get_pending_count("a1a1a1a1", "Alice") == 1

    @pytest.mark.asyncio
    async def test_send_bbs_prefix(self):
        msg = mock_message("bbs s Alice Hey", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        await self.cmd.execute(msg)
        assert self.cmd._get_pending_count("a1a1a1a1", "Alice") == 1

    @pytest.mark.asyncio
    async def test_send_bbs_send_prefix(self):
        msg = mock_message("bbs send Alice Hey", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        await self.cmd.execute(msg)
        assert self.cmd._get_pending_count("a1a1a1a1", "Alice") == 1

    @pytest.mark.asyncio
    async def test_send_bbs_msg_alias(self):
        msg = mock_message("bbs msg Alice Hey", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        await self.cmd.execute(msg)
        assert self.cmd._get_pending_count("a1a1a1a1", "Alice") == 1

    @pytest.mark.asyncio
    async def test_send_no_message_shows_shortlist(self):
        """'s Alice' with no message shows a shortlist and does NOT store a message."""
        msg = mock_message("s Alice", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        await self.cmd.execute(msg)
        assert self.cmd._get_pending_count("a1a1a1a1", "Alice") == 0
        self.bot.command_manager.send_response.assert_called_once()
        text = _last_response(self.bot)
        assert "Alice [a1a1]" in text
        assert "s <N> <message>" in text

    @pytest.mark.asyncio
    async def test_send_key_prefix_direct(self):
        msg = mock_message("s a1b2 Meet at noon.", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        _add_contact(self.bot.db_manager, "Keyed", public_key="a1b2c3d4")
        await self.cmd.execute(msg)
        assert self.cmd._get_pending_count("a1b2c3d4", "Keyed") == 1
        assert "Message stored for Keyed." in _last_response(self.bot)

    @pytest.mark.asyncio
    async def test_step1_no_match_shows_error(self):
        msg = mock_message("s Zzzzz", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        await self.cmd.execute(msg)
        assert "No contact found matching 'Zzzzz'." in _last_response(self.bot)

    @pytest.mark.asyncio
    async def test_step1_multiple_matches_shows_numbered_list(self):
        _add_contact(self.bot.db_manager, "Tom COD WP", public_key="aa0101")
        _add_contact(self.bot.db_manager, "Tom Jones", public_key="aa0202")
        msg = mock_message("s Tom", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        await self.cmd.execute(msg)
        text = _last_response(self.bot)
        assert "1 Tom COD WP [aa01]" in text
        assert "2 Tom Jones [aa02]" in text
        assert self.cmd._get_pending_count("aa0101", "Tom COD WP") == 0

    @pytest.mark.asyncio
    async def test_step1_single_match_with_message_sends_directly(self):
        msg = mock_message("s Alice hi", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        await self.cmd.execute(msg)
        assert self.cmd._get_pending_count("a1a1a1a1", "Alice") == 1

    @pytest.mark.asyncio
    async def test_step1_single_match_without_message_shows_shortlist(self):
        msg = mock_message("s Alice", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        await self.cmd.execute(msg)
        assert "Alice [a1a1]" in _last_response(self.bot)

    @pytest.mark.asyncio
    async def test_step2_sends_to_correct_candidate(self):
        _add_contact(self.bot.db_manager, "Tom COD WP", public_key="aa0101")
        _add_contact(self.bot.db_manager, "Tom Jones", public_key="aa0202")
        search = mock_message("s Tom", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        await self.cmd.execute(search)
        send = mock_message("s 2 Meet at noon.", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        await self.cmd.execute(send)
        assert self.cmd._get_pending_count("aa0101", "Tom COD WP") == 0
        assert self.cmd._get_pending_count("aa0202", "Tom Jones") == 1

    @pytest.mark.asyncio
    async def test_step2_clears_pending_after_send(self):
        msg1 = mock_message("s Tom", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        _add_contact(self.bot.db_manager, "Tom Jones", public_key="aa0202")
        await self.cmd.execute(msg1)
        await self.cmd.execute(mock_message("s 1 hi", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2"))
        assert self.cmd._get_pending_selection("b2b2b2b2") is None

    @pytest.mark.asyncio
    async def test_step2_without_pending_shows_error(self):
        msg = mock_message("s 1 orphan message", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        await self.cmd.execute(msg)
        assert "No pending recipient list." in _last_response(self.bot)

    @pytest.mark.asyncio
    async def test_step2_out_of_range_number_shows_error(self):
        msg1 = mock_message("s Alice", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        await self.cmd.execute(msg1)
        msg2 = mock_message("s 7 hi", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        await self.cmd.execute(msg2)
        assert "between 1 and 1" in _last_response(self.bot)

    @pytest.mark.asyncio
    async def test_step2_number_without_message_shows_usage(self):
        msg1 = mock_message("s Alice", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        await self.cmd.execute(msg1)
        msg2 = mock_message("s 1", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        await self.cmd.execute(msg2)
        assert "Usage: s 1 <message>" in _last_response(self.bot)

    @pytest.mark.asyncio
    async def test_step2_expired_pending_shows_error(self):
        msg1 = mock_message("s Alice", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        await self.cmd.execute(msg1)
        candidates, _ = self.cmd._pending_selections["b2b2b2b2"]
        self.cmd._pending_selections["b2b2b2b2"] = (candidates, time.time() - 1)
        msg2 = mock_message("s 1 late message", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        await self.cmd.execute(msg2)
        assert "No pending recipient list." in _last_response(self.bot)

    @pytest.mark.asyncio
    async def test_spaced_name_two_step_end_to_end(self):
        _add_contact(self.bot.db_manager, "Tom COD WP", public_key="aa0101")
        search = mock_message("s tom cod", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        await self.cmd.execute(search)
        send = mock_message("s 1 Hello Tom!", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        await self.cmd.execute(send)
        assert self.cmd._get_pending_count("aa0101", "Tom COD WP") == 1

    @pytest.mark.asyncio
    async def test_self_send_same_key_rejected(self):
        # Bob sends to himself by exact key prefix.
        msg = mock_message("s b2b2 hi", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        await self.cmd.execute(msg)
        assert "cannot send a message to yourself" in _last_response(self.bot)
        assert self.cmd._get_pending_count("b2b2b2b2", "Bob") == 0

    @pytest.mark.asyncio
    async def test_self_send_by_name_rejected(self):
        msg = mock_message("s Bob hi", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        await self.cmd.execute(msg)
        assert "cannot send a message to yourself" in _last_response(self.bot)

    @pytest.mark.asyncio
    async def test_self_send_prefix_vs_full_key_rejected(self):
        # sender_pubkey may be only a prefix when the resolver could not
        # fetch the full key — the guard must still catch it.
        _add_contact(self.bot.db_manager, "Bob", public_key="b2b2b2b2ffff")
        msg = mock_message("s b2b2b2b2ffff hi", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        await self.cmd.execute(msg)
        assert "cannot send a message to yourself" in _last_response(self.bot)

    @pytest.mark.asyncio
    async def test_rate_limit_rejects_extra_send(self):
        bot = _make_bot(rate_limit=2)
        cmd = BBSCommand(bot)
        bot.command_manager.send_response = AsyncMock(return_value=True)
        _add_contact(bot.db_manager, "Alice", public_key="a1a1a1a1")
        for _ in range(2):
            await cmd.execute(mock_message("s Alice hi", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2"))
        await cmd.execute(mock_message("s Alice third", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2"))
        assert "Too many BBS commands" in _last_response(bot)

    @pytest.mark.asyncio
    async def test_usage_when_no_args(self):
        msg = mock_message("s", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        await self.cmd.execute(msg)
        assert "Usage: s <name>" in _last_response(self.bot)


# ---------------------------------------------------------------------------
# Integration tests — execute() read / list / whoami / help
# ---------------------------------------------------------------------------


class TestBBSExecute:
    """Tests for the execute() method routing."""

    def setup_method(self):
        self.bot = _make_bot()
        self.cmd = BBSCommand(self.bot)
        self.bot.command_manager.send_response = AsyncMock(return_value=True)
        _add_contact(self.bot.db_manager, "Alice", public_key="a1a1a1a1")
        _add_contact(self.bot.db_manager, "Bob", public_key="b2b2b2b2")

    @pytest.mark.asyncio
    async def test_read_dm_returns_messages(self):
        self.cmd._store_message("a1a1a1a1", "Alice", "b2b2b2b2", "Bob", "Hi Bob!")
        msg = mock_message("bbs r", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        await self.cmd.execute(msg)
        text = _last_response(self.bot)
        assert "From Alice [" in text
        assert "Hi Bob!" in text
        # After reading, messages marked as read
        assert self.cmd._get_pending_count("b2b2b2b2", "Bob") == 0

    @pytest.mark.asyncio
    async def test_read_no_messages(self):
        msg = mock_message("bbs r", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        await self.cmd.execute(msg)
        self.bot.command_manager.send_response.assert_called_once()
        assert "No pending" in _last_response(self.bot)

    @pytest.mark.asyncio
    async def test_read_channel_rejected(self):
        msg = mock_message("bbs r", is_dm=False, sender_id="Bob", channel="general")
        await self.cmd.execute(msg)
        assert "only available via DM" in _last_response(self.bot)

    @pytest.mark.asyncio
    async def test_read_two_phase_keeps_messages_on_send_failure(self):
        self.cmd._store_message("a1a1a1a1", "Alice", "b2b2b2b2", "Bob", "Hi Bob!")
        self.bot.command_manager.send_response = AsyncMock(return_value=False)
        msg = mock_message("bbs r", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        await self.cmd.execute(msg)
        # Nothing committed: the message must still be pending for a retry.
        assert self.cmd._get_pending_count("b2b2b2b2", "Bob") == 1
        assert "messages kept" in _last_response(self.bot)

    @pytest.mark.asyncio
    async def test_read_chunks_long_inbox_per_message(self):
        long_body = "x" * 200
        self.cmd._store_message("a1a1a1a1", "Alice", "b2b2b2b2", "Bob", f"first {long_body}")
        self.cmd._store_message("a1a1a1a1", "Alice", "b2b2b2b2", "Bob", "second")
        msg = mock_message("bbs r", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        await self.cmd.execute(msg)
        responses = _all_responses(self.bot)
        # Each line sent separately, oldest first.
        assert len(responses) == 2
        assert "first" in responses[0]
        assert "second" in responses[1]
        assert self.cmd._get_pending_count("b2b2b2b2", "Bob") == 0

    @pytest.mark.asyncio
    async def test_read_includes_legacy_messages(self):
        _insert_legacy_message(self.bot.db_manager, "OldAlice", "Bob", "old school")
        self.cmd._store_message("a1a1a1a1", "Alice", "b2b2b2b2", "Bob", "new school")
        msg = mock_message("bbs r", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        await self.cmd.execute(msg)
        text = _last_response(self.bot)
        assert "old school" in text
        assert "new school" in text
        assert self.cmd._get_pending_count("b2b2b2b2", "Bob") == 0

    @pytest.mark.asyncio
    async def test_list_dm_shows_count(self):
        self.cmd._store_message("a1a1a1a1", "Alice", "b2b2b2b2", "Bob", "msg1")
        self.cmd._store_message("c3c3c3c3", "Carol", "b2b2b2b2", "Bob", "msg2")
        msg = mock_message("bbs list", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        await self.cmd.execute(msg)
        assert "2" in _last_response(self.bot)

    @pytest.mark.asyncio
    async def test_list_channel_rejected(self):
        msg = mock_message("bbs l", is_dm=False, sender_id="Bob", channel="general")
        await self.cmd.execute(msg)
        assert "only available via DM" in _last_response(self.bot)

    @pytest.mark.asyncio
    async def test_whoami_dm_shows_identity(self):
        self.cmd._store_message("a1a1a1a1", "Alice", "b2b2b2b2", "Bob", "msg1")
        msg = mock_message("bbs whoami", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        await self.cmd.execute(msg)
        text = _last_response(self.bot)
        assert "You are [Bob]" in text
        assert "key b2b2b2b2" in text
        assert "1 pending" in text

    @pytest.mark.asyncio
    async def test_whoami_alias_w(self):
        msg = mock_message("bbs w", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        await self.cmd.execute(msg)
        assert "You are [Bob]" in _last_response(self.bot)

    @pytest.mark.asyncio
    async def test_whoami_channel_rejected(self):
        msg = mock_message("bbs whoami", is_dm=False, sender_id="Bob", channel="general")
        await self.cmd.execute(msg)
        assert "only available via DM" in _last_response(self.bot)

    @pytest.mark.asyncio
    async def test_help_shown_for_bare_bbs(self):
        msg = mock_message("bbs", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        await self.cmd.execute(msg)
        assert "s <name>" in _last_response(self.bot)

    @pytest.mark.asyncio
    async def test_unknown_subcommand_shows_help(self):
        msg = mock_message("bbs unknowncmd", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        await self.cmd.execute(msg)
        assert "s <name>" in _last_response(self.bot)

    @pytest.mark.asyncio
    async def test_read_alias(self):
        msg = mock_message("bbs read", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        await self.cmd.execute(msg)
        assert "No pending" in _last_response(self.bot)


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
        self.cmd._store_message("a1a1a1a1", "Alice", "b2b2b2b2", "Bob", "Hi Bob!")
        msg = mock_message("hello", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        await self.cmd.check_inbox_notification(msg)
        self.bot.command_manager.send_response.assert_called_once()
        text = _last_response(self.bot)
        assert "pending" in text.lower()
        assert "bbs r" in text

    @pytest.mark.asyncio
    async def test_notification_sent_for_legacy_message(self):
        _insert_legacy_message(self.bot.db_manager, "Alice", "Bob", "hi")
        msg = mock_message("hello", is_dm=True, sender_id="Bob", sender_pubkey=None)
        await self.cmd.check_inbox_notification(msg)
        self.bot.command_manager.send_response.assert_called_once()

    @pytest.mark.asyncio
    async def test_notification_not_sent_when_no_messages(self):
        msg = mock_message("hello", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        await self.cmd.check_inbox_notification(msg)
        self.bot.command_manager.send_response.assert_not_called()

    @pytest.mark.asyncio
    async def test_notification_skipped_for_channel_message(self):
        self.cmd._store_message("a1a1a1a1", "Alice", "b2b2b2b2", "Bob", "hi")
        msg = mock_message("hello", is_dm=False, sender_id="Bob", channel="general")
        await self.cmd.check_inbox_notification(msg)
        self.bot.command_manager.send_response.assert_not_called()

    @pytest.mark.asyncio
    async def test_notification_skipped_on_bbs_read(self):
        self.cmd._store_message("a1a1a1a1", "Alice", "b2b2b2b2", "Bob", "hi")
        msg = mock_message("bbs r", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        await self.cmd.check_inbox_notification(msg)
        self.bot.command_manager.send_response.assert_not_called()

    @pytest.mark.asyncio
    async def test_notification_skipped_on_bbs_send(self):
        self.cmd._store_message("a1a1a1a1", "Alice", "b2b2b2b2", "Bob", "hi")
        msg = mock_message("s Alice hi", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        await self.cmd.check_inbox_notification(msg)
        self.bot.command_manager.send_response.assert_not_called()

    @pytest.mark.asyncio
    async def test_notification_cooldown_prevents_repeat(self):
        self.cmd._store_message("a1a1a1a1", "Alice", "b2b2b2b2", "Bob", "hi")
        msg = mock_message("hello", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        await self.cmd.check_inbox_notification(msg)
        assert self.bot.command_manager.send_response.call_count == 1

        # Second call immediately — should be blocked by cooldown
        await self.cmd.check_inbox_notification(msg)
        assert self.bot.command_manager.send_response.call_count == 1

    @pytest.mark.asyncio
    async def test_notification_resent_after_cooldown(self):
        self.cmd._store_message("a1a1a1a1", "Alice", "b2b2b2b2", "Bob", "hi")
        msg = mock_message("hello", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        # Pre-expire the cooldown
        self.cmd._notification_cooldowns["b2b2b2b2"] = time.time() - self.cmd.notify_cooldown_seconds - 1
        await self.cmd.check_inbox_notification(msg)
        assert self.bot.command_manager.send_response.call_count == 1

    @pytest.mark.asyncio
    async def test_default_cooldown_constant(self):
        assert self.cmd.notify_cooldown_seconds == NOTIFICATION_COOLDOWN_SECONDS

    @pytest.mark.asyncio
    async def test_disabled_command_skips_notification(self):
        bot = _make_bot(enabled=False)
        cmd = BBSCommand(bot)
        cmd._store_message("a1a1a1a1", "Alice", "b2b2b2b2", "Bob", "hi")
        msg = mock_message("hello", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        await cmd.check_inbox_notification(msg)
        bot.command_manager.send_response.assert_not_called()

    def test_can_execute_disabled(self):
        bot = _make_bot(enabled=False)
        cmd = BBSCommand(bot)
        msg = mock_message("bbs r", is_dm=True, sender_id="Bob")
        assert cmd.can_execute(msg) is False


# ---------------------------------------------------------------------------
# Unit tests — stage 1: arrival notification (private DM to the recipient)
# ---------------------------------------------------------------------------


class TestBBSArrivalNotification:
    def setup_method(self):
        self.bot = _make_bot()
        self.cmd = BBSCommand(self.bot)
        self.bot.command_manager.send_response = AsyncMock(return_value=True)
        self.bot.command_manager.send_dm = AsyncMock(return_value=True)
        _add_contact(self.bot.db_manager, "Alice", public_key="a1a1a1a1")
        _add_contact(self.bot.db_manager, "Bob", public_key="b2b2b2b2")

    @pytest.mark.asyncio
    async def test_execute_send_notifies_recipient_by_dm(self):
        msg = mock_message("s Alice Hello!", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2")
        await self.cmd.execute(msg)
        self.bot.command_manager.send_dm.assert_awaited_once()
        args = self.bot.command_manager.send_dm.call_args[0]
        assert args[0] == "a1a1a1a1"  # the recipient's key, not the sender's
        assert "1 pending" in args[1]
        assert "bbs r" in args[1]
        assert self.bot.command_manager.send_dm.call_args[1].get("skip_user_rate_limit") is True

    @pytest.mark.asyncio
    async def test_arrival_dm_is_aggregated_per_recipient(self):
        for i in range(3):
            self.cmd._store_message("a1a1", "A", "b2b2b2b2", "Bob", f"m{i}")
        await self.cmd._notify_recipient_arrival("b2b2b2b2", "Bob")
        self.bot.command_manager.send_dm.assert_awaited_once()
        assert "3 pending" in self.bot.command_manager.send_dm.call_args[0][1]

    @pytest.mark.asyncio
    async def test_arrival_notification_disabled_by_config(self):
        bot = _make_bot(notify_on_arrival=False)
        cmd = BBSCommand(bot)
        cmd._store_message("a1a1", "A", "b2b2b2b2", "Bob", "hi")
        await cmd._notify_recipient_arrival("b2b2b2b2", "Bob")
        bot.command_manager.send_dm.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_arrival_cooldown_absorbs_burst(self):
        self.cmd._store_message("a1a1", "A", "b2b2b2b2", "Bob", "hi")
        await self.cmd._notify_recipient_arrival("b2b2b2b2", "Bob")
        await self.cmd._notify_recipient_arrival("b2b2b2b2", "Bob")
        assert self.bot.command_manager.send_dm.await_count == 1

    @pytest.mark.asyncio
    async def test_failed_arrival_dm_is_not_locked_by_cooldown(self):
        self.bot.command_manager.send_dm = AsyncMock(return_value=False)
        self.cmd._store_message("a1a1", "A", "b2b2b2b2", "Bob", "hi")
        await self.cmd._notify_recipient_arrival("b2b2b2b2", "Bob")
        await self.cmd._notify_recipient_arrival("b2b2b2b2", "Bob")
        # Both attempts happen: a failed send must not consume the cooldown.
        assert self.bot.command_manager.send_dm.await_count == 2

    @pytest.mark.asyncio
    async def test_empty_recipient_key_sends_nothing(self):
        self.cmd._store_message("a1a1", "A", "", "Nokey", "hi")
        await self.cmd._notify_recipient_arrival("", "Nokey")
        self.bot.command_manager.send_dm.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failed_store_sends_no_notification(self):
        bot = _make_bot(max_messages=1)
        cmd = BBSCommand(bot)
        bot.command_manager.send_response = AsyncMock(return_value=True)
        _add_contact(bot.db_manager, "Alice", public_key="a1a1a1a1")
        await cmd.execute(mock_message("s Alice one", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2"))
        assert bot.command_manager.send_dm.await_count == 1
        await cmd.execute(mock_message("s Alice two", is_dm=True, sender_id="Bob", sender_pubkey="b2b2b2b2"))
        # Mailbox full → store refused → no second arrival DM.
        assert bot.command_manager.send_dm.await_count == 1
        assert "mailbox full" in _last_response(bot)


# ---------------------------------------------------------------------------
# Unit tests — stages 2 & 3: scheduler-driven reminder (24h) and final notice (73h)
# ---------------------------------------------------------------------------


class TestBBSStagedNotifications:
    def setup_method(self):
        self.bot = _make_bot()
        self.cmd = BBSCommand(self.bot)
        self.bot.command_manager.send_dm = AsyncMock(return_value=True)

    def _stamps(self, column: str, recipient_key: str = "b2b2b2b2") -> list:
        """Stage-column values for every row of *recipient_key*."""
        with self.bot.db_manager.connection() as conn:
            rows = conn.execute(
                f"SELECT {column} FROM bbs_inbox WHERE recipient_key = ?", (recipient_key,)
            ).fetchall()
        return [row[0] for row in rows]

    def _dm_texts(self) -> list[str]:
        return [call[0][1] for call in self.bot.command_manager.send_dm.call_args_list]

    @pytest.mark.asyncio
    async def test_reminder_after_24h(self):
        _insert_inbox_message(self.bot.db_manager, "a1a1", "b2b2b2b2", "old", sent_at=_old_ts(2))
        summary = await self.cmd.run_pending_notification_job()
        assert summary == {"sent": 1, "failed": 0}
        text = self._dm_texts()[0]
        assert "1 unread" in text
        assert "bbs r" in text
        assert all(self._stamps("reminder_sent_at"))
        assert all(s is None for s in self._stamps("final_notice_sent_at"))

    @pytest.mark.asyncio
    async def test_recent_messages_not_reminded(self):
        _insert_inbox_message(self.bot.db_manager, "a1a1", "b2b2b2b2", "fresh")
        summary = await self.cmd.run_pending_notification_job()
        assert summary == {"sent": 0, "failed": 0}
        self.bot.command_manager.send_dm.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reminder_counts_whole_mailbox_and_stamps_it(self):
        _insert_inbox_message(self.bot.db_manager, "a1a1", "b2b2b2b2", "old", sent_at=_old_ts(2))
        _insert_inbox_message(self.bot.db_manager, "a1a1", "b2b2b2b2", "joined recently")
        await self.cmd.run_pending_notification_job()
        assert "2 unread" in self._dm_texts()[0]
        # Both rows carry the stamp: one aggregate DM per recipient, not per message.
        assert all(self._stamps("reminder_sent_at"))

    @pytest.mark.asyncio
    async def test_reminder_not_repeated_for_stamped_rows(self):
        _insert_inbox_message(self.bot.db_manager, "a1a1", "b2b2b2b2", "old", sent_at=_old_ts(2))
        await self.cmd.run_pending_notification_job()
        second = await self.cmd.run_pending_notification_job()
        assert second == {"sent": 0, "failed": 0}
        assert self.bot.command_manager.send_dm.await_count == 1

    @pytest.mark.asyncio
    async def test_message_stored_after_a_reminder_gets_its_own_reminder(self):
        _insert_inbox_message(self.bot.db_manager, "a1a1", "b2b2b2b2", "old", sent_at=_old_ts(2))
        await self.cmd.run_pending_notification_job()
        # New arrival (already aged past 24h when stored, e.g. backlog import).
        _insert_inbox_message(self.bot.db_manager, "c3c3", "b2b2b2b2", "new old", sent_at=_old_ts(2))
        await self.cmd.run_pending_notification_job()
        assert self.bot.command_manager.send_dm.await_count == 2

    @pytest.mark.asyncio
    async def test_final_notice_after_73h_names_the_purge_age(self):
        _insert_inbox_message(self.bot.db_manager, "a1a1", "b2b2b2b2", "ancient", sent_at=_old_ts(4))
        summary = await self.cmd.run_pending_notification_job()
        assert summary == {"sent": 2, "failed": 0}  # reminder stage and final stage both due
        texts = self._dm_texts()
        assert any("unread" in t for t in texts)
        final = [t for t in texts if "final notice" in t]
        assert final
        assert "deleted after 7 days" in final[0]
        assert all(self._stamps("reminder_sent_at"))
        assert all(self._stamps("final_notice_sent_at"))

    @pytest.mark.asyncio
    async def test_final_stage_disabled_by_zero_hours(self):
        bot = _make_bot(final_hours=0)
        cmd = BBSCommand(bot)
        bot.command_manager.send_dm = AsyncMock(return_value=True)
        _insert_inbox_message(bot.db_manager, "a1a1", "b2b2b2b2", "old", sent_at=_old_ts(4))
        await cmd.run_pending_notification_job()
        assert bot.command_manager.send_dm.await_count == 1  # reminder only
        assert all(s is None for s in self._stamps("final_notice_sent_at"))

    @pytest.mark.asyncio
    async def test_reminder_stage_disabled_by_zero_hours(self):
        bot = _make_bot(reminder_hours=0)
        cmd = BBSCommand(bot)
        bot.command_manager.send_dm = AsyncMock(return_value=True)
        _insert_inbox_message(bot.db_manager, "a1a1", "b2b2b2b2", "old", sent_at=_old_ts(2))
        summary = await cmd.run_pending_notification_job()
        assert summary == {"sent": 0, "failed": 0}
        bot.command_manager.send_dm.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failed_dm_leaves_the_stage_unset_for_retry(self):
        self.bot.command_manager.send_dm = AsyncMock(return_value=False)
        _insert_inbox_message(self.bot.db_manager, "a1a1", "b2b2b2b2", "old", sent_at=_old_ts(2))
        summary = await self.cmd.run_pending_notification_job()
        assert summary == {"sent": 0, "failed": 1}
        assert all(s is None for s in self._stamps("reminder_sent_at"))
        # Next sweep retries once the radio is reachable again.
        self.bot.command_manager.send_dm = AsyncMock(return_value=True)
        summary = await self.cmd.run_pending_notification_job()
        assert summary == {"sent": 1, "failed": 0}

    @pytest.mark.asyncio
    async def test_send_dm_exception_counts_as_failure(self):
        self.bot.command_manager.send_dm = AsyncMock(side_effect=RuntimeError("radio down"))
        _insert_inbox_message(self.bot.db_manager, "a1a1", "b2b2b2b2", "old", sent_at=_old_ts(2))
        summary = await self.cmd.run_pending_notification_job()
        assert summary == {"sent": 0, "failed": 1}
        assert all(s is None for s in self._stamps("reminder_sent_at"))

    @pytest.mark.asyncio
    async def test_read_messages_are_not_reminded(self):
        _insert_inbox_message(self.bot.db_manager, "a1a1", "b2b2b2b2", "old", sent_at=_old_ts(2))
        self.cmd._mark_messages_read("b2b2b2b2", "Bob")
        summary = await self.cmd.run_pending_notification_job()
        assert summary == {"sent": 0, "failed": 0}
        self.bot.command_manager.send_dm.assert_not_awaited()

    def test_stage_helpers_reject_unknown_columns(self):
        # Column names are interpolated into SQL: only the two known stages pass.
        assert self.cmd._due_notification_recipients("read_at", 24) == []
        assert self.cmd._mark_notification_stage("b2b2b2b2", "message") == 0

    def test_stage_hours_defaults(self):
        assert self.cmd.notify_reminder_hours == DEFAULT_REMINDER_HOURS
        assert self.cmd.notify_final_hours == DEFAULT_FINAL_NOTICE_HOURS


# ---------------------------------------------------------------------------
# Tests — sender key identity
# ---------------------------------------------------------------------------


class TestBBSSenderKey:
    def test_pubkey_preferred(self):
        msg = mock_message("bbs", is_dm=True, sender_id="Bob", sender_pubkey="deadbeefcafe")
        assert BBSCommand._sender_key(msg) == "deadbeefcafe"

    def test_name_fallback_when_no_pubkey(self):
        msg = mock_message("bbs", is_dm=True, sender_id="Bob", sender_pubkey=None)
        assert BBSCommand._sender_key(msg) == "Bob"
