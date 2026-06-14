"""Tests for modules.commands.clock_command."""

from __future__ import annotations

import asyncio
import configparser
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from modules.commands.clock_command import ClockCommand
from tests.conftest import mock_message

ADMIN_PUBKEY = "a" * 64
OTHER_PUBKEY = "b" * 64


def _make_bot(*, enabled: bool = True, has_set_radio_clock: bool = True):
    bot = MagicMock()
    bot.logger = Mock()
    bot.connected = True
    bot.is_radio_zombie = False
    bot.is_radio_offline = False

    if has_set_radio_clock:
        bot.set_radio_clock = AsyncMock(return_value=True)
    else:
        del bot.set_radio_clock

    config = configparser.ConfigParser()
    config.add_section("Bot")
    config.set("Bot", "bot_name", "TestBot")
    config.add_section("Channels")
    config.set("Channels", "monitor_channels", "general")
    config.add_section("Keywords")
    config.add_section("Clock_Command")
    config.set("Clock_Command", "enabled", "true" if enabled else "false")
    config.add_section("Admin_ACL")
    config.set("Admin_ACL", "admin_pubkeys", ADMIN_PUBKEY)
    bot.config = config

    bot.translator = MagicMock()
    bot.translator.translate = Mock(side_effect=lambda key, **kw: key)
    bot.translator.get_value = Mock(return_value=None)
    bot.command_manager = MagicMock()
    bot.command_manager.monitor_channels = ["general"]
    bot.command_manager.send_response = AsyncMock(return_value=True)
    return bot


def _run(coro):
    return asyncio.run(coro)


class TestClockCommandPermissions:
    def test_dm_admin_allowed(self):
        cmd = ClockCommand(_make_bot())
        msg = mock_message(content="clock sync admin", is_dm=True, sender_id="admin")
        msg.sender_pubkey = ADMIN_PUBKEY
        assert cmd.can_execute(msg) is True

    def test_channel_disallowed(self):
        cmd = ClockCommand(_make_bot())
        msg = mock_message(content="clock sync admin", is_dm=False, sender_id="admin")
        msg.sender_pubkey = ADMIN_PUBKEY
        assert cmd.can_execute(msg) is False

    def test_non_admin_disallowed(self):
        cmd = ClockCommand(_make_bot())
        msg = mock_message(content="clock sync admin", is_dm=True, sender_id="other")
        msg.sender_pubkey = OTHER_PUBKEY
        assert cmd.can_execute(msg) is False

    def test_disabled_disallows(self):
        cmd = ClockCommand(_make_bot(enabled=False))
        msg = mock_message(content="clock sync admin", is_dm=True, sender_id="admin")
        msg.sender_pubkey = ADMIN_PUBKEY
        assert cmd.can_execute(msg) is False

    def test_keyword_matches_clock_sync_admin(self):
        cmd = ClockCommand(_make_bot())
        msg = mock_message(content="clock sync admin", is_dm=True, sender_id="admin")
        msg.sender_pubkey = ADMIN_PUBKEY
        assert cmd.matches_keyword(msg) is True

    def test_keyword_matches_bare_clock(self):
        cmd = ClockCommand(_make_bot())
        msg = mock_message(content="clock", is_dm=True, sender_id="admin")
        msg.sender_pubkey = ADMIN_PUBKEY
        assert cmd.matches_keyword(msg) is True

    def test_keyword_does_not_match_other_command(self):
        cmd = ClockCommand(_make_bot())
        msg = mock_message(content="ping", is_dm=True, sender_id="admin")
        msg.sender_pubkey = ADMIN_PUBKEY
        assert cmd.matches_keyword(msg) is False


class TestClockCommandExecution:
    def test_successful_sync_sends_ok(self):
        bot = _make_bot()
        bot.set_radio_clock = AsyncMock(return_value=True)
        cmd = ClockCommand(bot)
        msg = mock_message(content="clock sync admin", is_dm=True, sender_id="admin")
        msg.sender_pubkey = ADMIN_PUBKEY

        result = _run(cmd.execute(msg))

        assert result is True
        bot.set_radio_clock.assert_awaited_once()
        call_args = bot.command_manager.send_response.call_args
        assert "✓" in str(call_args)

    def test_failed_sync_sends_error(self):
        bot = _make_bot()
        bot.set_radio_clock = AsyncMock(return_value=False)
        cmd = ClockCommand(bot)
        msg = mock_message(content="clock sync admin", is_dm=True, sender_id="admin")
        msg.sender_pubkey = ADMIN_PUBKEY

        result = _run(cmd.execute(msg))

        assert result is True
        call_args = bot.command_manager.send_response.call_args
        assert "✗" in str(call_args)

    def test_exception_in_sync_sends_error(self):
        bot = _make_bot()
        bot.set_radio_clock = AsyncMock(side_effect=RuntimeError("radio boom"))
        cmd = ClockCommand(bot)
        msg = mock_message(content="clock sync admin", is_dm=True, sender_id="admin")
        msg.sender_pubkey = ADMIN_PUBKEY

        result = _run(cmd.execute(msg))

        assert result is True
        call_args = bot.command_manager.send_response.call_args
        assert "✗" in str(call_args)

    def test_missing_set_radio_clock_sends_unavailable(self):
        bot = _make_bot(has_set_radio_clock=False)
        cmd = ClockCommand(bot)
        msg = mock_message(content="clock sync admin", is_dm=True, sender_id="admin")
        msg.sender_pubkey = ADMIN_PUBKEY

        result = _run(cmd.execute(msg))

        assert result is True
        call_args = bot.command_manager.send_response.call_args
        assert "not available" in str(call_args).lower()

    def test_requires_admin_access_returns_true(self):
        cmd = ClockCommand(_make_bot())
        assert cmd.requires_admin_access() is True
