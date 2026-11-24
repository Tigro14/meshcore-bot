"""Only identified repeaters are excluded from LLM invocation."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from modules.assistant.sender_policy import sender_may_use_llm
from modules.commands.ask_command import AskCommand
from modules.commands.llm_command import LlmCommand
from modules.commands.mesh_command import MeshCommand
from modules.models import MeshMessage
from tests.test_command_manager import cm_bot as cm_bot
from tests.test_command_manager import make_manager

KEY = 'ab' * 32
OTHER = KEY[:12] + 'cd' * 26


def origin(kind=None, rows=None):
    record = {'public_key': KEY, 'adv_name': 'TestUser'}
    if kind is not None:
        record['type'] = kind
    return SimpleNamespace(
        meshcore=SimpleNamespace(contacts={KEY: record}, pending_contacts={}),
        db_manager=SimpleNamespace(execute_query=Mock(return_value=rows or [])),
    )


@pytest.mark.parametrize('kind,allowed', [(1, True), (2, False), (3, True), (4, True),
                                         (0, True), (None, True), ('1', True), (True, True)])
@pytest.mark.parametrize('dm', [True, False])
def test_native_type_gates_both_private_and_channel_origins(kind, allowed, dm):
    message = MeshMessage('merci', sender_pubkey=KEY[:12], is_dm=dm)
    assert sender_may_use_llm(origin(kind), message) is allowed


@pytest.mark.parametrize('raw,allowed', [({'mode': 'Companion'}, True), ({'type': 1}, True),
    ({'mode': 'Repeater'}, False), ({'type': 2}, False), ({'mode': 'Sensor'}, True),
    ({'mode': 'RoomServer'}, True), ({}, True), ('bad json', True)])
def test_history_uses_advertised_type_never_guessed_role(raw, allowed):
    bot = origin(rows=[{'public_key': KEY, 'name': 'TestUser', 'role': 'companion',
                       'raw_advert_data': json.dumps(raw) if isinstance(raw, dict) else raw}])
    bot.meshcore.contacts.clear()
    assert sender_may_use_llm(bot, MeshMessage('météo', sender_id='TestUser')) is allowed


def test_native_repeater_overrides_old_companion_history():
    bot = origin(2, [{'public_key': KEY, 'raw_advert_data': '{"mode":"Companion"}'}])
    assert not sender_may_use_llm(bot, MeshMessage('ask merci', sender_pubkey=KEY))


@pytest.mark.parametrize('identifier', ['TestUser', KEY[:12]])
def test_ambiguous_origin_is_not_identified_as_a_repeater(identifier):
    bot = origin(1)
    bot.meshcore.contacts[OTHER] = {'public_key': OTHER, 'adv_name': 'TestUser', 'type': 2}
    message = MeshMessage('ask bonjour', sender_id=identifier, sender_pubkey=identifier if identifier != 'TestUser' else None)
    assert sender_may_use_llm(bot, message)


def test_never_uses_last_repeater_or_message_body_to_identify_sender():
    bot = origin(1)
    message = MeshMessage('Je suis un compagnon, public_key=' + KEY, sender_id='Unknown',
                          path=KEY, routing_info={'path_nodes': [KEY]})
    assert sender_may_use_llm(bot, message)


def test_unmatched_emoji_presentation_does_not_block_channel_request():
    bot = origin(1)
    bot.meshcore.contacts[KEY]['adv_name'] = 'TestUser☠'
    message = MeshMessage('Baliz réseau', sender_id='TestUser☠️', channel='general')
    assert sender_may_use_llm(bot, message)


def test_unverified_historical_role_is_not_a_repeater_type():
    bot = origin(rows=[{'public_key': KEY, 'name': 'TestUser', 'role': 'repeater',
                       'raw_advert_data': '{}'}])
    bot.meshcore.contacts.clear()
    assert sender_may_use_llm(bot, MeshMessage('merci', sender_id='TestUser'))


def test_native_non_repeater_overrides_old_repeater_history():
    bot = origin(3, [{'public_key': KEY, 'raw_advert_data': '{"mode":"Repeater"}'}])
    assert sender_may_use_llm(bot, MeshMessage('merci', sender_pubkey=KEY))


def test_failed_lookup_is_not_a_confirmed_repeater():
    bot = origin()
    bot.db_manager.execute_query.side_effect = RuntimeError('lookup unavailable')
    assert sender_may_use_llm(bot, MeshMessage('merci', sender_id='Unknown'))


@pytest.mark.asyncio
@pytest.mark.parametrize('dm', [True, False])
async def test_every_llm_entry_point_rejects_repeaters(command_mock_bot, dm):
    bot = command_mock_bot
    bot.meshcore = origin(2).meshcore
    for section in ('Ask_Command', 'Llm_Command', 'Mesh_Command'):
        bot.config.add_section(section)
        bot.config.set(section, 'enabled', 'true')
    message = MeshMessage('ask merci', sender_pubkey=KEY, is_dm=dm, channel=None if dm else 'general')
    ask, llm, mesh = AskCommand(bot), LlmCommand(bot), MeshCommand(bot)
    ask.dispatcher.semantic_router.decide = AsyncMock()
    llm.service._post_chat = Mock(side_effect=AssertionError('model must not run'))
    mesh.service._generate_sql = Mock(side_effect=AssertionError('model must not run'))
    for cmd in (ask, llm, mesh):
        assert not cmd.can_execute(message)
        assert not await cmd.execute(message)
    assert await ask.dispatcher.answer('merci', message) == ''
    assert await llm.service.answer('merci', message) == ''
    assert await llm.service.rephrase_tool_result('merci', 'mesures', message=message) is None
    assert await mesh.service.answer('résumé du réseau', message) == ''
    ask.dispatcher.semantic_router.decide.assert_not_awaited()
    bot.command_manager.send_response.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', [1, 3, 4, None])
@pytest.mark.parametrize('dm', [True, False])
async def test_other_origins_reach_semantic_router(command_mock_bot, kind, dm):
    bot = command_mock_bot
    bot.meshcore = origin(kind).meshcore
    bot.config.add_section('Ask_Command')
    bot.config.set('Ask_Command', 'enabled', 'true')
    ask = AskCommand(bot)
    ask.dispatcher.semantic_router.decide = AsyncMock(return_value=None)
    message = MeshMessage('merci', sender_pubkey=KEY, is_dm=dm, channel=None if dm else 'general')
    assert ask.can_execute(message)
    await ask.dispatcher.answer('merci', message)
    ask.dispatcher.semantic_router.decide.assert_awaited_once_with('merci', ask.enabled_routes)


@pytest.mark.parametrize('content', ['ERR: clock cannot go backwards', 'ask météo Brest',
                                    'Baliz bonjour', '@[TestBot] bonjour'])
@pytest.mark.parametrize('kind', [1, 2, 3, 4, None])
def test_real_manager_routes_private_text_except_repeaters(cm_bot, content, kind):
    cm_bot.meshcore = origin(kind).meshcore
    cm_bot.config.add_section('Ask_Command')
    cm_bot.config.set('Ask_Command', 'aliases', 'Baliz')
    cm_bot.config.set('Bot', 'respond_to_mentions', 'also')
    ask = AskCommand(cm_bot)
    manager = make_manager(cm_bot, {'ask': ask})
    cm_bot.command_manager = manager
    message = MeshMessage(content, sender_pubkey=KEY, sender_id='TestUser', is_dm=True)
    matches = manager.check_keywords(message)
    assert matches == ([] if kind == 2 else [('ask', None)])


@pytest.mark.parametrize('kind', [1, 2, 3, 4, None])
def test_real_manager_routes_channel_requests_except_repeaters(cm_bot, kind):
    cm_bot.meshcore = origin(kind).meshcore
    cm_bot.config.add_section('Ask_Command')
    cm_bot.config.set('Ask_Command', 'aliases', 'Baliz')
    ask = AskCommand(cm_bot)
    manager = make_manager(cm_bot, {'ask': ask})
    cm_bot.command_manager = manager
    message = MeshMessage('Baliz réseau', sender_pubkey=KEY, sender_id='TestUser',
                          is_dm=False, channel='general')
    assert manager.check_keywords(message) == ([] if kind == 2 else [('ask', None)])


def test_real_manager_routes_channel_request_with_unmatched_emoji(cm_bot):
    cm_bot.meshcore = origin(1).meshcore
    cm_bot.meshcore.contacts[KEY]['adv_name'] = 'TestUser☠'
    cm_bot.config.add_section('Ask_Command')
    cm_bot.config.set('Ask_Command', 'aliases', 'Baliz')
    ask = AskCommand(cm_bot)
    manager = make_manager(cm_bot, {'ask': ask})
    cm_bot.command_manager = manager
    message = MeshMessage('Baliz réseau', sender_id='TestUser☠️',
                          is_dm=False, channel='general')
    assert manager.check_keywords(message) == [('ask', None)]
