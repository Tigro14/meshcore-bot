"""Exercise the public DM pipeline, per-message reporting and cleanup exclusion."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from meshcore import EventType
from meshcore.events import Event

from modules.repeater_manager import RepeaterManager
from tests.conftest import mock_message
from tests.test_command_manager import cm_bot as cm_bot
from tests.test_command_manager import make_manager
from tests.test_dm_contact_resolution import KEY, contact


@pytest.fixture
def pipeline(cm_bot):
    cm_bot.connected = True
    cm_bot.per_user_rate_limit_enabled = False
    cm_bot.meshcore = SimpleNamespace(contacts={KEY: contact()}, pending_contacts={}, commands=SimpleNamespace(
        send_msg_with_retry=AsyncMock(return_value=Event(EventType.MSG_SENT, {})),
        send_msg=AsyncMock(), get_contact_by_key=AsyncMock(return_value=Event(EventType.ERROR, {})),
        get_contacts=AsyncMock(return_value=Event(EventType.CONTACTS, {})),
        remove_contact=AsyncMock(return_value=Event(EventType.OK, {})),
    ))
    cm_bot.bot_tx_rate_limiter.wait_for_tx = AsyncMock()
    async def radio_lookup(key):
        record = cm_bot.meshcore.contacts.get(key.hex())
        return Event(EventType.NEXT_CONTACT, record) if record else Event(EventType.ERROR, {})
    cm_bot.meshcore.commands.get_contact_by_key.side_effect = radio_lookup
    manager = make_manager(cm_bot)
    return manager, cm_bot


@pytest.mark.asyncio
async def test_ack_is_attached_to_the_original_message(pipeline):
    manager, bot = pipeline
    message = mock_message(is_dm=True, sender_pubkey=KEY)
    assert await manager.send_response(message, 'Bonjour')
    assert message.dm_delivery == {'state': 'acknowledged', 'reason': 'ack_received'}
    sent = bot.meshcore.commands.send_msg_with_retry.await_args.args[0]
    assert sent['public_key'] == KEY


@pytest.mark.asyncio
async def test_missing_contact_is_not_an_ack_failure(pipeline):
    manager, bot = pipeline
    bot.meshcore.contacts.clear()
    report = {}
    assert not await manager.send_dm(KEY, 'Bonjour', delivery_report=report)
    assert report['state'] == 'not_sent'
    bot.meshcore.commands.send_msg_with_retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_ack_is_uncertain_delivery(pipeline):
    manager, bot = pipeline
    bot.meshcore.commands.send_msg_with_retry.return_value = None
    report = {}
    assert not await manager.send_dm(KEY, 'Bonjour', delivery_report=report)
    assert report == {'state': 'unconfirmed', 'reason': 'ack_missing'}


@pytest.mark.asyncio
async def test_retry_internal_error_never_triggers_a_second_uncontrolled_send(pipeline):
    manager, bot = pipeline
    bot.meshcore.commands.send_msg_with_retry.side_effect = AttributeError('internal library error')
    report = {}
    assert not await manager.send_dm(KEY, 'Bonjour', delivery_report=report)
    assert report['state'] == 'unconfirmed'
    bot.meshcore.commands.send_msg.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_releases_contact_protection(pipeline):
    manager, bot = pipeline
    started = asyncio.Event()
    async def sending(*args, **kwargs):
        started.set()
        await asyncio.Future()
    bot.meshcore.commands.send_msg_with_retry.side_effect = sending
    task = asyncio.create_task(manager.send_dm(KEY, 'Bonjour'))
    await started.wait()
    assert manager.dm_contacts.is_in_use(KEY)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not manager.dm_contacts.is_in_use(KEY)


@pytest.mark.asyncio
async def test_cleanup_cannot_delete_the_current_recipient(pipeline):
    manager, bot = pipeline
    repeater = RepeaterManager.__new__(RepeaterManager)
    repeater.bot = bot;repeater.logger = Mock();repeater._purge_inflight_keys = set()
    async with manager.dm_contacts.use(KEY):
        assert not await repeater.purge_repeater_from_contacts(KEY)
        assert not await repeater.purge_companion_from_contacts(KEY)
        assert not await repeater.purge_repeater_by_contact_key(KEY)
        result = await repeater._remove_contact_from_radio(KEY)
        assert result.type == EventType.ERROR
    bot.meshcore.commands.remove_contact.assert_not_awaited()


@pytest.mark.asyncio
async def test_cleanup_already_waiting_on_the_radio_blocks_dm_preparation(pipeline):
    manager, bot = pipeline
    repeater = RepeaterManager.__new__(RepeaterManager)
    repeater.bot = bot;repeater.logger = Mock();repeater._purge_inflight_keys = set()
    bot.repeater_manager = repeater
    started = asyncio.Event()
    async def removing(*args):
        started.set();await asyncio.Future()
    bot.meshcore.commands.remove_contact.side_effect = removing
    task = asyncio.create_task(repeater._remove_contact_from_radio(KEY))
    await started.wait()
    report = {}
    assert not await manager.send_dm(KEY, 'Bonjour', delivery_report=report)
    assert report['reason'] == 'contact_cleanup_in_progress'
    bot.meshcore.commands.send_msg_with_retry.assert_not_awaited()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert not repeater._purge_inflight_keys


@pytest.mark.asyncio
async def test_clock_does_not_wait_for_a_reply_when_no_frame_was_emitted():
    from configparser import ConfigParser

    from modules.scheduler import MessageScheduler
    bot = Mock()
    bot.config = ConfigParser()
    bot.config.read_dict({'Bot': {}, 'Clock_Sync_Admin': {'enabled': 'true', 'targets': KEY}})
    bot.connected = True;bot.is_radio_offline = False;bot.is_radio_zombie = False
    scheduler = MessageScheduler(bot)
    scheduler._get_clock_sync_targets = Mock(return_value=[KEY])
    scheduler._resolve_clock_sync_target_contact = Mock(return_value={'public_key': KEY, 'name': 'Peer'})
    scheduler._get_clock_sync_target_drift = Mock(return_value=None)
    scheduler._get_clock_sync_target_row = Mock(return_value=None)
    scheduler._log_clock_sync_admin_attempt = Mock()
    scheduler._wait_for_clock_sync_reply = AsyncMock()
    async def not_sent(*args, delivery_report, **kwargs):
        delivery_report.update(state='not_sent', reason='contact_table_full')
        return False
    bot.command_manager.send_dm = AsyncMock(side_effect=not_sent)
    result = await scheduler._run_clock_sync_admin_job_async()
    assert result['failed'] == 1 and result['sent'] == 0
    scheduler._wait_for_clock_sync_reply.assert_not_awaited()
    assert 'contact_table_full' in scheduler._log_clock_sync_admin_attempt.call_args.args[3]


@pytest.mark.asyncio
async def test_split_dm_reports_partial_delivery_and_stops_after_unconfirmed_chunk(pipeline, monkeypatch):
    manager, bot = pipeline
    bot.meshcore.commands.send_msg_with_retry.side_effect = [Event(EventType.MSG_SENT, {}), None]
    monkeypatch.setattr('modules.command_manager.asyncio.sleep', AsyncMock())
    report = {}
    assert not await manager.send_dm(KEY, 'x' * 400, delivery_report=report)
    assert report == {'state': 'partial', 'reason': 'ack_missing'}
    assert bot.meshcore.commands.send_msg_with_retry.await_count == 2


def test_configured_clock_target_never_picks_the_first_ambiguous_name_or_prefix(pipeline):
    from modules.scheduler import MessageScheduler
    from tests.test_dm_contact_resolution import OTHER
    manager, bot = pipeline
    bot.meshcore.contacts[OTHER] = contact(OTHER)
    scheduler = MessageScheduler(bot)
    assert scheduler._resolve_clock_sync_target_contact('Peer') is None
    assert scheduler._resolve_clock_sync_target_contact(KEY[:12]) is None
    assert scheduler._resolve_clock_sync_target_contact(KEY)['public_key'] == KEY


@pytest.mark.asyncio
async def test_successful_cleanup_invalidates_only_the_removed_radio_contact(pipeline):
    from tests.test_dm_contact_resolution import OTHER
    manager, bot = pipeline
    manager.dm_contacts._cache(contact())
    bot.meshcore.contacts[OTHER] = contact(OTHER)
    repeater = RepeaterManager.__new__(RepeaterManager)
    repeater.bot = bot;repeater.logger = Mock();repeater._purge_inflight_keys = set()
    assert (await repeater._remove_contact_from_radio(KEY)).type == EventType.OK
    assert KEY not in bot.meshcore.contacts
    assert KEY not in manager.dm_contacts._verified
    assert OTHER in bot.meshcore.contacts
