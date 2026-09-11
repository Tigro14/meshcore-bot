"""Tests for modules.commands.multibyte_command."""

import configparser
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, Mock

from modules.commands.multibyte_command import MultibyteCommand
from tests.conftest import mock_message
from tests.test_multibyte_detection import _add_repeater, _make_db


def _make_bot(db_path):
    bot = MagicMock()
    bot.logger = Mock()
    config = configparser.ConfigParser()
    config.add_section("Bot")
    config.set("Bot", "bot_name", "TestBot")
    config.add_section("Channels")
    config.set("Channels", "monitor_channels", "general")
    config.set("Channels", "respond_to_dms", "true")
    config.add_section("Keywords")
    config.add_section("Multibyte_Command")
    config.set("Multibyte_Command", "enabled", "true")
    bot.config = config
    bot.translator = MagicMock()
    bot.translator.translate = Mock(side_effect=lambda key, **kw: key)
    bot.command_manager = MagicMock()
    bot.command_manager.monitor_channels = ["general"]

    # Plain SQLite connection to the test DB (no migration machinery).
    @contextmanager
    def _connection():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    db_manager = MagicMock()
    db_manager.connection = _connection
    bot.db_manager = db_manager
    return bot


def _seed(db_path):
    now = datetime.now()
    conn = _make_db(db_path)
    _add_repeater(conn, "1122334455667788", "Alpha", tracked=1, obph=1, opl=0, adv=10, last=str(now))
    _add_repeater(conn, "2222334455667788", "Bravo", tracked=1, obph=2, opl=0, adv=20, last=str(now))
    conn.commit()
    conn.close()


async def test_execute_lists_one_byte_only(tmp_path):
    db_path = str(tmp_path / "mb.db")
    _seed(db_path)
    bot = _make_bot(db_path)
    cmd = MultibyteCommand(bot)

    sent = []
    cmd.send_response = AsyncMock(side_effect=lambda msg, text: sent.append(text))

    await cmd.execute(mock_message(content="multibyte"))
    assert len(sent) == 1
    out = sent[0]
    assert "Alpha" in out
    assert "Bravo" not in out
    assert "of 2 tracked" in out


async def test_execute_all_multibyte(tmp_path):
    db_path = str(tmp_path / "mb.db")
    now = datetime.now()
    conn = _make_db(db_path)
    _add_repeater(conn, "2222334455667788", "Bravo", tracked=1, obph=2, opl=0, adv=20, last=str(now))
    conn.commit()
    conn.close()
    bot = _make_bot(db_path)
    cmd = MultibyteCommand(bot)

    sent = []
    cmd.send_response = AsyncMock(side_effect=lambda msg, text: sent.append(text))
    await cmd.execute(mock_message(content="multibyte"))
    assert "All 1 currently-tracked repeaters are multibyte" in sent[0]


async def test_execute_top_arg(tmp_path):
    db_path = str(tmp_path / "mb.db")
    now = datetime.now()
    conn = _make_db(db_path)
    _add_repeater(conn, "1122334455667788", "Alpha", tracked=1, obph=1, opl=0, adv=10, last=str(now))
    _add_repeater(
        conn,
        "3322334455667788",
        "Charlie",
        tracked=1,
        obph=1,
        opl=0,
        adv=5,
        last=str(now - timedelta(hours=2)),
    )
    conn.commit()
    conn.close()
    bot = _make_bot(db_path)
    cmd = MultibyteCommand(bot)

    sent = []
    cmd.send_response = AsyncMock(side_effect=lambda msg, text: sent.append(text))
    await cmd.execute(mock_message(content="multibyte 1"))
    out = sent[0]
    assert "Alpha" in out
    assert "Charlie" not in out


async def test_disabled_command_not_executed(tmp_path):
    db_path = str(tmp_path / "mb.db")
    _seed(db_path)
    bot = _make_bot(db_path)
    bot.config.set("Multibyte_Command", "enabled", "false")
    cmd = MultibyteCommand(bot)
    assert cmd.can_execute(mock_message(content="multibyte")) is False
