"""Interleaved commands retain their own reply and transmission identity."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tests import test_command_manager as manager_fixtures
from tests.conftest import mock_message
from tests.test_command_manager import make_manager

cm_bot = manager_fixtures.cm_bot


def manager_with_command(bot, execute):
    bot.transmission_tracker = None
    command = SimpleNamespace(
        name="ask",
        is_channel_allowed=lambda _: True,
        should_execute=lambda _: True,
        get_response_format=lambda: None,
        can_execute_now=lambda _: True,
        requires_internet=False,
        last_response="",
        execute=execute,
    )
    manager = make_manager(bot, {"ask": command})
    manager._should_queue_command = Mock(return_value=(False, 0))
    manager.record_command_stat = Mock()
    viewer = SimpleNamespace(capture_command=Mock())
    manager._viewer_bridge = lambda: viewer
    return manager, viewer


@pytest.mark.parametrize("same_sender", [False, True])
@pytest.mark.parametrize("is_dm", [False, True])
async def test_interleaved_replies_do_not_cross_questions_even_while_waiting_for_delivery(cm_bot, same_sender, is_dm):
    started = {name: asyncio.Event() for name in ["source", "forecast"]}
    release = {name: asyncio.Event() for name in ["source", "forecast"]}
    wire = {}

    async def execute(message):
        return await manager.send_response(message, "reply-" + message.content)

    async def send(recipient, content, command_id, **kwargs):
        name = content.removeprefix("reply-")
        wire[name] = command_id
        started[name].set()
        await release[name].wait()
        return name == "source"

    manager, viewer = manager_with_command(cm_bot, execute)
    if is_dm:
        manager.send_dm = AsyncMock(side_effect=send)
    else:
        manager.send_channel_message = AsyncMock(side_effect=send)
    first = mock_message("source", sender_id="First", is_dm=is_dm)
    second = mock_message("forecast", sender_id="First" if same_sender else "Second", is_dm=is_dm)
    a = asyncio.create_task(manager.execute_commands(first))
    await asyncio.wait_for(started["source"].wait(), 2)
    b = asyncio.create_task(manager.execute_commands(second))
    await asyncio.wait_for(started["forecast"].wait(), 2)
    release["source"].set()
    await asyncio.wait_for(a, 2)
    release["forecast"].set()
    await asyncio.wait_for(b, 2)
    captured = {
        call.args[0].original_content or call.args[0].content: call.args
        for call in viewer.capture_command.call_args_list
    }
    assert captured["source"][2] == "reply-source"
    assert captured["forecast"][2] == "reply-forecast"
    assert captured["source"][3] is True and captured["forecast"][3] is False
    assert captured["source"][4] == wire["source"]
    assert captured["forecast"][4] == wire["forecast"]
    assert wire["source"] and wire["forecast"] and wire["source"] != wire["forecast"]


async def test_a_command_without_a_reply_never_reuses_another_users_text(cm_bot):
    manager, viewer = manager_with_command(cm_bot, AsyncMock(return_value=False))
    manager._last_response = "Private reply belonging to somebody else"
    await manager.execute_commands(mock_message("source", is_dm=True))
    assert viewer.capture_command.call_args.args[2] == "Command executed"
    assert manager.record_command_stat.call_args.args[2] is False


async def test_chunked_dm_has_one_command_identity_and_its_own_full_reply(cm_bot):
    async def execute(message):
        return await manager.send_response_chunked(message, ["part one", "part two"])

    manager, viewer = manager_with_command(cm_bot, execute)
    manager.send_dm = AsyncMock(return_value=True)
    manager._chunk_spacing_seconds = lambda: 0
    cm_bot.bot_tx_rate_limiter.wait_for_tx = AsyncMock()
    await manager.execute_commands(mock_message("source", is_dm=True))
    assert viewer.capture_command.call_args.args[2] == "part one\npart two"
    identifier = viewer.capture_command.call_args.args[4]
    assert all(call.kwargs["command_id"] == identifier for call in manager.send_dm.call_args_list)


async def test_rendered_tool_output_is_not_recorded_as_the_users_actual_reply(cm_bot):
    async def execute(message):
        rendered = mock_message("render only", is_dm=True)
        rendered.capture_sink = []
        await manager.send_response(rendered, "Internal tool output")
        assert rendered.capture_sink == ["Internal tool output"]
        return await manager.send_response(message, "Actual answer")

    manager, viewer = manager_with_command(cm_bot, execute)
    manager.send_dm = AsyncMock(return_value=True)
    await manager.execute_commands(mock_message("source", is_dm=True))
    assert viewer.capture_command.call_args.args[2] == "Actual answer"
    assert manager.send_dm.await_count == 1


async def test_reply_context_is_reset_after_a_command_exception(cm_bot):
    async def execute(message):
        raise RuntimeError("simulated failure")

    manager, viewer = manager_with_command(cm_bot, execute)
    manager.commands["ask"].translate = lambda *args, **kwargs: "Failure reply"
    manager.send_dm = AsyncMock(return_value=True)
    await manager.execute_commands(mock_message("source", is_dm=True))
    await manager.send_response(mock_message("outside", is_dm=True), "Unrelated reply")
    assert manager.send_dm.await_args.args[2] is None
    assert viewer.capture_command.call_args.args[2] == "Error: simulated failure"


async def test_explicit_transmission_identity_is_preserved(cm_bot):
    async def execute(message):
        return await manager.send_response(message, "Answer", command_id="explicit-identifier")

    manager, viewer = manager_with_command(cm_bot, execute)
    manager.send_dm = AsyncMock(return_value=True)
    await manager.execute_commands(mock_message("source", is_dm=True))
    assert viewer.capture_command.call_args.args[4] == "explicit-identifier"
    assert manager.send_dm.await_args.args[2] == "explicit-identifier"


async def test_legacy_reply_correlation_cannot_take_another_recipients_identical_text(cm_bot):
    manager, viewer = manager_with_command(cm_bot, AsyncMock(return_value=True))
    manager.commands["ask"].last_response = "Identical answer"
    import time

    stamp = time.time()
    wrong = SimpleNamespace(
        content="Identical answer", target="Other", message_type="dm", timestamp=stamp, command_id="other-command"
    )
    right = SimpleNamespace(
        content="Identical answer", target="First", message_type="dm", timestamp=stamp, command_id="legacy"
    )
    cm_bot.transmission_tracker = SimpleNamespace(
        pending_transmissions={int(stamp): [wrong, right]}, confirmed_transmissions={}
    )
    await manager.execute_commands(mock_message("source", sender_id="First", is_dm=True))
    assert wrong.command_id == "other-command"
    assert right.command_id == viewer.capture_command.call_args.args[4]
