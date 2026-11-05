"""Delayed radio channel replies must not mislabel or publish another channel."""

import asyncio
import configparser
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from meshcore import EventType

from modules.channel_manager import ChannelManager
from modules.models import MeshMessage
from modules.web_viewer.channel_visibility import channel_identity, set_visibility
from modules.web_viewer.integration import BotIntegration
from tests.test_web_viewer_app import viewer_with_db as viewer_with_db

PUBLIC_KEY = bytes.fromhex("8b3387e9c5cdea6ac9e5edbaa115cd72")


def event(index, name, key=PUBLIC_KEY):
    return SimpleNamespace(
        type=EventType.CHANNEL_INFO,
        payload={
            "channel_idx": index,
            "channel_name": name,
            "channel_secret": key,
        },
    )


@pytest.fixture
def manager():
    bot = SimpleNamespace(
        logger=Mock(),
        config=configparser.ConfigParser(),
        connected=True,
        meshcore=SimpleNamespace(commands=SimpleNamespace(get_channel=AsyncMock()), channels={}),
        db_manager=Mock(),
    )
    result = ChannelManager(bot, max_channels=6)
    result._fetch_interval = 0
    result._store_channels_in_db = Mock()
    return result


@pytest.mark.asyncio
async def test_public_read_is_retried_after_transient_timeout(manager):
    manager.bot.meshcore.commands.get_channel.side_effect = [asyncio.TimeoutError(), event(0, "Public")]
    result = await manager._fetch_single_channel(0)
    assert result and result["channel_idx"] == 0 and result["channel_name"] == "Public"
    assert manager.bot.meshcore.commands.get_channel.await_count == 2


@pytest.mark.asyncio
async def test_late_reply_for_another_slot_is_not_accepted(manager):
    manager.bot.meshcore.commands.get_channel.side_effect = [event(2, "Private", b"\xab" * 16), event(0, "Public")]
    result = await manager._fetch_single_channel(0)
    assert result["channel_idx"] == 0 and result["channel_name"] == "Public"
    assert manager.bot.meshcore.commands.get_channel.await_count == 2


@pytest.mark.asyncio
async def test_persistent_wrong_slot_fails_closed_with_bounded_retries(manager):
    manager.bot.meshcore.commands.get_channel.return_value = event(2, "Private", b"\xab" * 16)
    assert await manager._fetch_single_channel(0) is None
    assert manager.bot.meshcore.commands.get_channel.await_count == 2


@pytest.mark.asyncio
async def test_confirmed_empty_slot_is_not_retried(manager):
    manager.bot.meshcore.commands.get_channel.return_value = event(1, "", b"\x00" * 16)
    assert await manager._fetch_single_channel(1) is None
    assert manager.bot.meshcore.commands.get_channel.await_count == 1


@pytest.mark.asyncio
async def test_refresh_keeps_current_cache_until_scan_completes(manager):
    original = dict(event(0, "Public").payload)
    manager._channels_cache = {0: original}
    manager._cache_valid = True

    async def fetch(index):
        assert manager.get_channel_name(0) == "Public"
        return original if index == 0 else None

    manager._fetch_single_channel = fetch
    result = await manager.fetch_all_channels(force_refresh=True)
    assert result == [original]
    assert manager.get_channel_name(0) == "Public"


@pytest.mark.asyncio
async def test_valid_public_scan_preserves_visibility_without_unhiding_private_channels(manager, viewer_with_db):
    attempts = {}

    async def read(index):
        attempts[index] = attempts.get(index, 0) + 1
        if index == 0 and attempts[index] == 1:
            raise asyncio.TimeoutError()
        if index == 0:
            return event(0, "Public")
        if index == 2:
            return event(2, "Private", b"\xab" * 16)
        return event(index, "", b"\x00" * 16)

    manager.bot.meshcore.commands.get_channel.side_effect = read
    with patch("modules.channel_manager.asyncio.sleep", new_callable=AsyncMock):
        channels = await manager.fetch_all_channels(force_refresh=True)
    assert {row["channel_idx"] for row in channels} == {0, 2}
    assert manager.get_channel_name(0) == "Public"
    viewer = viewer_with_db
    import sqlite3

    with sqlite3.connect(viewer.db_path) as conn:
        for row in channels:
            conn.execute(
                "INSERT INTO channels(channel_idx,channel_name,channel_type,channel_key_hex) VALUES(?,?,?,?)",
                (row["channel_idx"], row["channel_name"], "custom", row["channel_key_hex"]),
            )
        set_visibility(conn, channel_identity("Public", PUBLIC_KEY.hex()), True)

    capture = object.__new__(BotIntegration)
    capture._get_web_viewer_db_path = lambda: viewer.db_path
    capture._insert_packet_stream_row = Mock()
    capture.bot = Mock()
    for index, visible in [(0, True), (2, False)]:
        capture.capture_channel_message(
            MeshMessage(
                content="local test body", sender_id="node", channel=manager.get_channel_name(index), is_dm=False
            )
        )
        data = json.loads(capture._insert_packet_stream_row.call_args.args[0])
        assert data["channel_visibility_id"] is not None
        assert viewer._can_publish_channel_data(data) is visible


@pytest.mark.asyncio
async def test_public_read_retries_a_real_wait_for_deadline(manager):
    # Exercise asyncio's own timeout type on every supported Python version.
    manager._fetch_timeout = 0.05
    calls = 0

    async def read(index):
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.Event().wait()
        return event(index, "Public")

    manager.bot.meshcore.commands.get_channel.side_effect = read
    result = await manager._fetch_single_channel(0)
    assert result and result["channel_idx"] == 0 and result["channel_name"] == "Public"
    assert calls == 2
