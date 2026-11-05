"""DM preparation never sends to an ambiguous, absent or unverified restored node."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from meshcore import EventType
from meshcore.events import Event

from modules.dm_contacts import DmContactResolver

KEY = 'aabbccddee01' + '01' * 26
OTHER = 'aabbccddee01' + '02' * 26


def contact(key=KEY, name='Peer'):
    return dict(public_key=key, adv_name=name, type=1, flags=1, last_advert=1793800000,
                adv_lat=48.1, adv_lon=-3.1, out_path='11223344', out_path_len=2, out_path_hash_mode=1)


def history(key=KEY, name='Peer'):
    return dict(public_key=key, name=name, role='companion', raw_advert_data=json.dumps({
        'public_key': key, 'name': name, 'mode': 'Companion', 'signature': 'ab' * 64,
        'advert_time': 1793800000, 'lat': 48.1, 'lon': -3.1,
        'out_path': '11223344', 'out_path_len': 2, 'out_bytes_per_hop': 2,
    }))


@pytest.fixture
def setup():
    rows = []
    db = Mock()

    def query(sql, params):
        value = params[0]
        return [r for r in rows if (r['public_key'].startswith(value[:-1]) if value.endswith('%')
                                    else r['name'].lower() == value)]
    db.execute_query.side_effect = query
    commands = SimpleNamespace(
        get_contact_by_key=AsyncMock(return_value=Event(EventType.ERROR, {})),
        get_contacts=AsyncMock(return_value=Event(EventType.CONTACTS, {})),
        add_contact=AsyncMock(return_value=Event(EventType.OK, {})),
    )
    bot = SimpleNamespace(db_manager=db, logger=Mock(), repeater_manager=SimpleNamespace(_purge_inflight_keys=set(), contact_limit=350),
                          meshcore=SimpleNamespace(contacts={}, pending_contacts={}, commands=commands))
    resolver = DmContactResolver(bot)
    return resolver, bot, rows, commands


@pytest.mark.asyncio
async def test_cached_full_identity_preserves_radio_path_and_flags(setup):
    resolver, bot, rows, commands = setup
    stored = contact()
    bot.meshcore.contacts[KEY] = stored
    rows.append(history())
    commands.get_contact_by_key.return_value = Event(EventType.NEXT_CONTACT, stored)
    async with resolver.use(KEY.upper()) as result:
        assert result.contact == stored
        assert result.contact['out_path'] == '11223344'
        assert result.contact['flags'] == 1
        assert resolver.is_in_use(KEY)
    assert not resolver.is_in_use(KEY)
    commands.get_contacts.assert_not_awaited()
    commands.add_contact.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('identifier', [KEY[:12], 'Peer'])
async def test_ambiguity_is_rejected_even_if_only_one_candidate_is_cached(setup, identifier):
    resolver, bot, rows, commands = setup
    bot.meshcore.contacts[KEY] = contact()
    rows.extend([history(), history(OTHER)])
    async with resolver.use(identifier) as result:
        assert result.contact is None and result.reason == 'ambiguous_contact'
    commands.get_contacts.assert_not_awaited()
    commands.add_contact.assert_not_awaited()


@pytest.mark.asyncio
async def test_full_key_ignores_duplicate_display_names(setup):
    resolver, bot, rows, commands = setup
    bot.meshcore.contacts = {KEY: contact(), OTHER: contact(OTHER)}
    rows.extend([history(), history(OTHER)])
    commands.get_contact_by_key.return_value = Event(EventType.NEXT_CONTACT, contact())
    async with resolver.use(KEY) as result:
        assert result.contact['public_key'] == KEY


@pytest.mark.asyncio
async def test_single_radio_read_recovers_missing_cache(setup):
    resolver, bot, rows, commands = setup
    commands.get_contact_by_key.return_value = Event(EventType.NEXT_CONTACT, contact())
    async with resolver.use(KEY) as result:
        assert result.reason == 'radio_read'
        assert result.contact['public_key'] == KEY
    commands.add_contact.assert_not_awaited()
    commands.get_contacts.assert_not_awaited()


@pytest.mark.asyncio
async def test_late_reply_with_another_key_is_never_used(setup):
    resolver, bot, rows, commands = setup
    commands.get_contact_by_key.return_value = Event(EventType.NEXT_CONTACT, contact(OTHER))
    async with resolver.use(KEY) as result:
        assert result.contact is None
    assert KEY not in bot.meshcore.contacts
    commands.add_contact.assert_not_awaited()


@pytest.mark.asyncio
async def test_full_refresh_finds_the_target_without_restoration(setup):
    resolver, bot, rows, commands = setup
    commands.get_contacts.return_value = Event(EventType.CONTACTS, {KEY: contact()})
    async with resolver.use(KEY) as result:
        assert result.reason == 'radio_refresh'
        assert result.contact['public_key'] == KEY
    commands.add_contact.assert_not_awaited()


@pytest.mark.asyncio
async def test_restore_signed_history_then_verify_without_copying_announcement_path(setup):
    resolver, bot, rows, commands = setup
    rows.append(history())
    commands.get_contact_by_key.side_effect = [Event(EventType.ERROR, {}), Event(EventType.NEXT_CONTACT, contact())]
    async with resolver.use(KEY[:12]) as result:
        assert result.reason == 'radio_restored'
    saved = commands.add_contact.await_args.args[0]
    assert saved['public_key'] == KEY
    assert saved['out_path'] == '' and saved['out_path_len'] == -1
    assert saved['adv_name'] == 'Peer' and saved['type'] == 1
    assert commands.get_contact_by_key.await_count == 2


@pytest.mark.asyncio
async def test_pending_contact_must_be_registered_and_read_back(setup):
    resolver, bot, rows, commands = setup
    bot.meshcore.pending_contacts[KEY] = contact()
    commands.get_contact_by_key.side_effect = [Event(EventType.ERROR, {}), Event(EventType.NEXT_CONTACT, contact())]
    async with resolver.use(KEY[:12]) as result:
        assert result.reason == 'radio_restored'
    commands.add_contact.assert_awaited_once()
    assert commands.add_contact.await_args.args[0]['flags'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('failure,reason', [
    (Event(EventType.ERROR, {'error_code': 3}), 'contact_table_full'),
    (Event(EventType.ERROR, {'error_code': 9}), 'contact_restore_failed'),
])
async def test_failed_restore_is_not_accepted_and_does_not_purge_others(setup, failure, reason):
    resolver, bot, rows, commands = setup
    rows.append(history())
    commands.add_contact.return_value = failure
    async with resolver.use(KEY) as result:
        assert result.contact is None and result.reason == reason
    assert KEY not in bot.meshcore.contacts


@pytest.mark.asyncio
async def test_update_ok_alone_is_not_delivery_readiness(setup):
    resolver, bot, rows, commands = setup
    rows.append(history())
    async with resolver.use(KEY) as result:
        assert result.contact is None and result.reason == 'contact_restore_unconfirmed'
    assert KEY not in bot.meshcore.contacts


@pytest.mark.asyncio
async def test_missing_announcement_does_not_invent_a_contact(setup):
    resolver, bot, rows, commands = setup
    rows.append(dict(public_key=KEY, name='Peer', role='companion', raw_advert_data='{}'))
    async with resolver.use(KEY) as result:
        assert result.contact is None and result.reason == 'contact_data_incomplete'
    commands.add_contact.assert_not_awaited()


@pytest.mark.asyncio
async def test_timeout_cannot_trigger_an_unverified_overwrite(setup):
    resolver, bot, rows, commands = setup
    rows.append(history())
    commands.get_contacts.side_effect = asyncio.TimeoutError()
    async with resolver.use(KEY) as result:
        assert result.contact is None and result.reason == 'contact_resolution_timeout'
    commands.add_contact.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_cleanup_blocks_dm_preparation(setup):
    resolver, bot, rows, commands = setup
    bot.repeater_manager._purge_inflight_keys.add(KEY)
    async with resolver.use(KEY) as result:
        assert result.contact is None and result.reason == 'contact_cleanup_in_progress'
    commands.add_contact.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_requests_for_same_missing_node_restore_it_once(setup):
    resolver, bot, rows, commands = setup
    rows.append(history())
    reads = 0
    async def read(key):
        nonlocal reads
        reads += 1
        return Event(EventType.ERROR, {}) if reads == 1 else Event(EventType.NEXT_CONTACT, contact())
    commands.get_contact_by_key.side_effect = read
    async def prepare():
        async with resolver.use(KEY) as result:
            assert result.contact is not None
            await asyncio.sleep(0)
    await asyncio.gather(prepare(), prepare())
    commands.add_contact.assert_awaited_once()
    assert not resolver._active and not resolver._locks


def test_malformed_or_mismatched_stored_announcement_is_rejected():
    row = history();raw = json.loads(row['raw_advert_data']);raw['public_key'] = OTHER
    row['raw_advert_data'] = json.dumps(raw)
    assert DmContactResolver.restoration(row, None, KEY) is None
    row = history();raw = json.loads(row['raw_advert_data']);raw['signature'] = 'invalid'
    row['raw_advert_data'] = json.dumps(raw)
    assert DmContactResolver.restoration(row, None, KEY) is None


@pytest.mark.asyncio
async def test_manual_contact_management_is_not_overridden(setup):
    from configparser import ConfigParser
    resolver, bot, rows, commands = setup
    rows.append(history())
    bot.config = ConfigParser()
    bot.config.read_dict({'Bot': {'auto_manage_contacts': 'false'}})
    async with resolver.use(KEY) as result:
        assert result.reason == 'contact_restore_disabled'
    commands.add_contact.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_refresh_does_not_trust_a_stale_radio_cache(setup):
    resolver, bot, rows, commands = setup
    bot.meshcore.contacts[KEY] = contact()
    commands.get_contacts.return_value = Event(EventType.ERROR, {})
    async with resolver.use(KEY) as result:
        assert result.contact is None and result.reason == 'contact_refresh_failed'
    commands.add_contact.assert_not_awaited()


@pytest.mark.asyncio
async def test_simultaneous_missing_nodes_share_one_complete_refresh(setup):
    resolver, bot, rows, commands = setup
    commands.get_contacts.return_value = Event(EventType.CONTACTS, {KEY: contact(), OTHER: contact(OTHER)})
    async def prepare(key):
        async with resolver.use(key) as result:
            assert result.contact['public_key'] == key
    await asyncio.gather(prepare(KEY), prepare(OTHER))
    commands.get_contacts.assert_awaited_once()
    commands.add_contact.assert_not_awaited()


@pytest.mark.asyncio
async def test_radio_read_uses_the_existing_serial_session(setup):
    from contextlib import asynccontextmanager
    resolver, bot, rows, commands = setup
    held = False
    @asynccontextmanager
    async def session():
        nonlocal held
        assert not held
        held = True
        try:yield
        finally:held = False
    bot.radio_session = session
    async def read(key):
        assert held
        return Event(EventType.NEXT_CONTACT, contact())
    commands.get_contact_by_key.side_effect = read
    async with resolver.use(KEY) as result:
        assert result.contact is not None
    assert not held


@pytest.mark.asyncio
async def test_full_radio_table_is_not_overwritten(setup):
    resolver, bot, rows, commands = setup
    rows.append(history())
    bot.repeater_manager.contact_limit = 1
    commands.get_contacts.return_value = Event(EventType.CONTACTS, {OTHER: contact(OTHER)})
    async with resolver.use(KEY) as result:
        assert result.reason == 'contact_table_full'
    commands.add_contact.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_capacity_does_not_risk_overwriting_another_contact(setup):
    resolver, bot, rows, commands = setup
    rows.append(history())
    bot.repeater_manager.contact_limit = None
    async with resolver.use(KEY) as result:
        assert result.reason == 'contact_capacity_unknown'
    commands.add_contact.assert_not_awaited()
