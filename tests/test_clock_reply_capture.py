"""Real dispatcher events; no radio transmissions or real-time minute waits."""

import asyncio
from configparser import ConfigParser
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from meshcore.events import Event, EventDispatcher, EventType

from modules.clock_reply import ClockReplyWatch
from modules.scheduler import MessageScheduler

KEY = 'ab' * 32
OTHER = 'cd' * 32


def setup():
    config = ConfigParser()
    config.read_dict({'Bot': {}, 'Clock_Sync_Admin': {
        'enabled': 'true', 'targets': KEY, 'clock_sync_admin_reply_window_seconds': '0.02',
    }})
    dispatcher = EventDispatcher()
    bot = SimpleNamespace(config=config, logger=Mock(), connected=True,
        is_radio_offline=False, is_radio_zombie=False, db_manager=None,
        meshcore=SimpleNamespace(subscribe=dispatcher.subscribe, contacts={
            KEY: {'public_key': KEY, 'name': 'Target', 'type': 2},
        }, pending_contacts={}), command_manager=SimpleNamespace(send_dm=AsyncMock()))
    scheduler = MessageScheduler(bot)
    scheduler._get_clock_sync_target_drift = Mock(return_value=None)
    scheduler._get_clock_sync_target_row = Mock(return_value=None)
    scheduler._log_clock_sync_admin_attempt = Mock()
    return bot, scheduler, dispatcher


def response(key=KEY, text='ERR: clock cannot go backwards', stamp=1):
    return Event(EventType.CONTACT_MSG_RECV, {'pubkey_prefix': key[:12],
                 'text': text, 'sender_timestamp': stamp}, {'pubkey_prefix': key[:12]})


@pytest.mark.asyncio
@pytest.mark.parametrize('ack', [True, False])
async def test_reply_during_ack_wait_is_retained_even_with_wrong_remote_clock(ack):
    bot, scheduler, dispatcher = setup()
    await dispatcher.start()
    async def send(*args, **kwargs):
        assert len(dispatcher.subscriptions) == 1  # listening BEFORE first send
        await dispatcher.dispatch(response())
        await dispatcher.queue.join()
        await asyncio.sleep(0.001)  # ACK arrives later, or never arrives
        return ack
    bot.command_manager.send_dm.side_effect = send
    try:
        result = await scheduler._run_clock_sync_admin_job_async()
        assert result['sent'] == 1 and result['failed'] == 0
        scheduler._log_clock_sync_admin_attempt.assert_called_once_with(
            KEY, 'Target', True, 'ERR: clock cannot go backwards')
        assert dispatcher.subscriptions == []
    finally:
        await dispatcher.stop()


@pytest.mark.asyncio
async def test_reply_arriving_after_final_ack_wait_is_also_accepted():
    bot, scheduler, dispatcher = setup()
    await dispatcher.start()
    async def late_reply():
        await asyncio.sleep(0.005)
        await dispatcher.dispatch(response(text='Clock synced'))
    task = asyncio.create_task(late_reply())
    bot.command_manager.send_dm.return_value = False
    try:
        result = await scheduler._run_clock_sync_admin_job_async()
        assert result['sent'] == 1
        assert dispatcher.subscriptions == []
    finally:
        await task
        await dispatcher.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('ambiguous', [True, False])
async def test_wrong_or_ambiguous_sender_never_confirms_delivery(ambiguous):
    bot, _, dispatcher = setup()
    key = KEY[:12] + 'ef' * 26 if ambiguous else OTHER
    bot.meshcore.contacts[key] = {'public_key': key, 'type': 2}
    with ClockReplyWatch(bot, KEY) as replies:
        replies._receive(response(key))
        assert await replies.wait(0.001) == (False, '')
    assert dispatcher.subscriptions == []


@pytest.mark.asyncio
async def test_reply_from_before_subscription_is_not_replayed():
    bot, _, dispatcher = setup()
    await dispatcher.start()
    try:
        await dispatcher.dispatch(response())
        await dispatcher.queue.join()
        with ClockReplyWatch(bot, KEY) as replies:
            assert await replies.wait(0.001) == (False, '')
    finally:
        await dispatcher.stop()


@pytest.mark.asyncio
async def test_cancelled_send_always_unsubscribes():
    bot, scheduler, dispatcher = setup()
    started = asyncio.Event()
    async def pending(*args, **kwargs):
        started.set()
        await asyncio.Future()
    bot.command_manager.send_dm.side_effect = pending
    task = asyncio.create_task(scheduler._run_clock_sync_admin_job_async())
    await started.wait()
    assert dispatcher.subscriptions
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert dispatcher.subscriptions == []


def test_scheduler_deadline_covers_multiple_targets_and_optional_clkreboot():
    _, scheduler, _ = setup()
    budget = scheduler._clock_sync_timeout(3)
    assert budget > 3 * (2 * (20 + 3 * 30) + 60)
