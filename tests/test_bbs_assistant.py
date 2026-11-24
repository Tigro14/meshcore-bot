"""BBS routing, recipient conversations and real delivery boundary (radio mocked)."""
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from modules.assistant.bbs_service import BBSAssistant
from modules.assistant.catalog import CATALOG, parse_plan
from modules.assistant.dispatcher import AssistantDispatcher
from modules.assistant.router import Decision, Route
from modules.commands.ask_command import AskCommand
from modules.commands.bbs_command import BBSCommand
from tests.commands.test_bbs_command import _TRACKED_CONNECTIONS, _add_contact, _make_bot
from tests.conftest import mock_message

ALICE, BOB, BOB2 = 'a1' * 32, 'b2' * 32, 'c3' * 32


@pytest.fixture
def environment():
    bot = _make_bot()
    command = BBSCommand(bot)
    bot.command_manager.send_response = AsyncMock(return_value=True)
    bot.command_manager.send_dm = AsyncMock(return_value=True)
    _add_contact(bot.db_manager, 'Alice', public_key=ALICE)
    _add_contact(bot.db_manager, 'Bob Alpha', public_key=BOB)
    _add_contact(bot.db_manager, 'Bob Bravo', public_key=BOB2)
    dispatcher = SimpleNamespace(_command=lambda name: command,
                                 _allowed=lambda cmd, message: cmd.can_execute(message),
                                 record_function=Mock())
    service = BBSAssistant(dispatcher)
    yield service, command, bot
    while _TRACKED_CONNECTIONS:
        _TRACKED_CONNECTIONS.pop().close()


def message(text='question', key=ALICE, dm=True, channel=None):
    return mock_message(text, sender_id='Alice', sender_pubkey=key, is_dm=dm, channel=channel)


def plan(operation, **args):
    return Decision(Route.BBS, 'question', 'catalog', operation, args)


async def test_send_keeps_literal_text_and_notifies(environment):
    service, bbs, bot = environment
    text = 'Météo Brest ? Non, rendez-vous à 12h !'
    assert await service.execute(plan('send', recipient='Bob Alpha', text=text), message()) is None
    assert bbs._get_pending_messages_by_key(BOB)[0][2] == text
    assert not service.pending
    assert 'Message déposé pour Bob Alpha' in bot.command_manager.send_response.call_args.args[1]
    assert bot.command_manager.send_dm.call_args.args[0] == BOB


async def test_ambiguous_name_keeps_body_until_choice(environment):
    service, bbs, _ = environment
    response = await service.execute(plan('send', recipient='Bob', text='À demain !'), message())
    assert 'Bob Alpha' in response and 'Bob Bravo' in response
    context = service.context(message())
    assert context['text_already_provided'] is True
    assert 'À demain !' not in json.dumps(context)
    await service.execute(plan('continue', selection='Alpha', text=''), message('Alpha'))
    assert bbs._get_pending_messages_by_key(BOB)[0][2] == 'À demain !'
    assert not bbs._get_pending_messages_by_key(BOB2)


async def test_can_supply_recipient_then_body_in_three_turns(environment):
    service, bbs, _ = environment
    assert 'À quel' in await service.execute(plan('send', recipient='', text=''), message())
    assert 'Quel message' in await service.execute(plan('continue', selection='Bob Alpha', text=''), message())
    await service.execute(plan('continue', selection='', text='Bonjour !'), message('Bonjour !'))
    assert bbs._get_pending_messages_by_key(BOB)[0][2] == 'Bonjour !'


async def test_shortlist_number_and_same_name_users_are_isolated(environment):
    service, bbs, _ = environment
    await service.execute(plan('send', recipient='Bob', text='secret'), message())
    assert service.context(message(key=BOB)) is None
    result = await service.execute(plan('continue', selection='1', text=''), message(key=BOB))
    assert 'Aucun envoi' in result
    assert not bbs._get_pending_messages_by_key(BOB)
    await service.execute(plan('continue', selection='1', text=''), message('1'))
    assert sum(len(bbs._get_pending_messages_by_key(k)) for k in [BOB, BOB2]) == 1


async def test_channel_context_cannot_be_used_from_dm(environment):
    service, bbs, _ = environment
    await service.execute(plan('send', recipient='Bob', text='Bonjour'), message(dm=False, channel='general'))
    assert service.pending
    assert service.context(message()) is None
    assert not bbs._get_pending_messages_by_key(BOB)


async def test_expired_cancelled_or_unknown_selection_never_sends(environment):
    service, bbs, _ = environment
    await service.execute(plan('send', recipient='Bob', text='Bonjour'), message())
    assert 'Je ne trouve' in await service.execute(plan('continue', selection='Absent', text=''), message())
    assert not bbs._get_pending_messages_by_key(BOB)
    for state in service.pending.values():
        state.expires = time.monotonic() - 1
    assert 'expirée' in await service.execute(plan('continue', selection='1', text=''), message())
    await service.execute(plan('send', recipient='Bob', text='Bonjour'), message())
    await service.execute(plan('cancel'), message())
    assert not service.pending


async def test_count_and_read_remain_private_and_preserve_failed_delivery(environment):
    service, bbs, bot = environment
    bbs._store_message(BOB, 'Bob', ALICE, 'Alice', 'Contenu privé')
    for operation in ['count', 'read']:
        assert 'en privé' in await service.execute(plan(operation), message(dm=False, channel='general'))
    assert '1 message' in await service.execute(plan('count'), message())
    bot.command_manager.send_response.return_value = False
    await service.execute(plan('read'), message())
    assert bbs._get_pending_count(ALICE, 'Alice') == 1
    bot.command_manager.send_response.return_value = True
    await service.execute(plan('read'), message())
    assert bbs._get_pending_count(ALICE, 'Alice') == 0


async def test_disabled_capture_self_send_and_quotas(environment):
    service, bbs, _ = environment
    bbs.enabled = False
    assert 'indisponible' in await service.execute(plan('send', recipient='Bob Alpha', text='x'), message())
    bbs.enabled = True
    msg = message()
    msg.capture_sink = []
    assert 'échange direct' in await service.execute(plan('read'), msg)
    assert 'propre nœud' in await service.execute(plan('send', recipient='Alice', text='x'), message())
    bbs.max_messages_per_user = 1
    await service.execute(plan('send', recipient='Bob Alpha', text='x'), message())
    assert 'pleine' in await service.execute(plan('send', recipient='Bob Alpha', text='y'), message())
    assert len(bbs._get_pending_messages_by_key(BOB)) == 1


@pytest.mark.parametrize('operation,args,question', [
    ('send', {'recipient': 'Bob', 'text': 'inventé'}, 'Écris à Bob : bonjour'),
    ('send', {'recipient': 'Autre', 'text': 'bonjour'}, 'Écris à Bob : bonjour'),
    ('send', {'recipient': 'Bob', 'text': 'Bonjour'}, 'Écris à Bob : bonjour'),
    ('continue', {'selection': '1', 'text': ''}, 'Alpha'),
    ('read', {'key': BOB}, 'Lis les messages de Bob'),
])
def test_model_cannot_invent_rewrite_or_read_someone_else(operation, args, question):
    with pytest.raises(ValueError):
        parse_plan(json.dumps({'route': 'bbs', 'operation': operation, 'args': args}), question, set(CATALOG))


async def test_ask_does_not_send_empty_reply_after_bbs_delivery(environment):
    _service, _bbs, bot = environment
    bot.config.add_section('Ask_Command')
    ask = AskCommand(bot)
    ask.dispatcher.answer = AsyncMock(return_value=None)
    await ask.execute(message('ask lis mes messages'))
    bot.command_manager.send_response.assert_not_called()


def test_notification_offers_natural_reading_when_enabled(environment):
    _service, bbs, bot = environment
    bot.command_manager.commands = {'ask': SimpleNamespace(enabled_routes={'bbs'})}
    assert 'lis mes messages' in bbs._arrival_text(1)
    assert 'lis mes messages' in bbs._reminder_text(1)
    assert '7 jours' in bbs._final_notice_text(1)


async def test_dispatcher_passes_only_pending_metadata_to_router(environment):
    service, bbs, bot = environment
    dispatcher = object.__new__(AssistantDispatcher)
    dispatcher.owner = SimpleNamespace(bot=bot, enabled_routes={'bbs'}, logger=Mock(), route_timeout_seconds=2)
    dispatcher.bbs = service
    dispatcher.semantic_router = SimpleNamespace(decide=AsyncMock(return_value=plan('count')))
    dispatcher._dispatch = AsyncMock(return_value='ok')
    await service.execute(plan('send', recipient='Bob', text='Texte jamais repris dans le contexte'), message())
    assert await dispatcher.answer('Alpha', message('Alpha')) == 'ok'
    kwargs = dispatcher.semantic_router.decide.call_args.kwargs
    assert 'bbs_context' in kwargs and 'Texte jamais' not in str(kwargs)


@pytest.mark.parametrize('question,extracted,expected', [
    ('Dis à Alpha : météo Brest demain ?', 'météo Brest demain', 'météo Brest demain ?'),
    ('Laisse à Alpha : rendez-vous à midi.', 'rendez-vous à midi', 'rendez-vous à midi.'),
    ('Dis à Alpha « Bonjour ! »', 'Bonjour !', 'Bonjour !'),
])
def test_message_text_comes_from_source_with_terminal_punctuation(question, extracted, expected):
    decision = parse_plan(json.dumps({'route':'bbs','operation':'send',
        'args':{'recipient':'Alpha','text':extracted}}), question, set(CATALOG))
    assert decision.args['text'] == expected


async def test_ask_extracts_literal_body_before_mention_cleanup(environment):
    _service, _bbs, bot = environment
    bot.config.add_section('Ask_Command')
    bot.config.set('Ask_Command', 'aliases', 'Baliz')
    ask = AskCommand(bot)
    ask.dispatcher.answer = AsyncMock(return_value=None)
    msg = message('Baliz Dis à Bob : Salut @[Cam] !  À midi.')
    msg.content = 'ask Dis à Bob : Salut ! À midi.'
    await ask.execute(msg)
    assert ask.dispatcher.answer.call_args.args[0] == 'Dis à Bob : Salut @[Cam] !  À midi.'


def test_pending_body_schema_copies_entire_current_reply():
    from unittest.mock import patch

    from modules.assistant.semantic_router import SemanticRouter
    router = SemanticRouter(SimpleNamespace(logger=Mock(), get_config_value=lambda *a, fallback=None, **k: fallback))
    body = 'Salut @[Cam] !  À demain.'
    response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
        'choices': [{'message': {'content': json.dumps({'function': 'bbs.continue', 'args': {'selection': '', 'text': body}})}}]})
    with patch('modules.assistant.semantic_router.post_chat', return_value=response) as post:
        result = router._classify(body, set(CATALOG), {'awaiting':'text','recipient':'Bob Alpha',
                                'candidates':['Bob Alpha'],'text_already_provided':False})
    assert result.args['text'] == body
    variants = post.call_args.args[1]['response_format']['json_schema']['schema']['anyOf']
    schema = next(v['properties']['args'] for v in variants if v['properties']['function']['const'] == 'bbs.continue')
    assert schema['properties']['text']['const'] == body
    assert schema['properties']['selection']['const'] == ''


async def test_own_node_is_rejected_before_asking_for_body(environment):
    service, bbs, bot = environment
    response = await service.execute(plan('send', recipient='Alice', text=''), message())
    assert 'propre nœud' in response
    assert service.context(message())['awaiting'] == 'recipient'
    assert not bbs._get_pending_messages_by_key(ALICE)
    assert 'Quel message' in await service.execute(plan('continue', selection='Bob Alpha', text=''), message())
    bot.command_manager.send_dm.assert_not_awaited()


def real_assistant_manager(bot, bbs):
    from modules.command_manager import CommandManager
    if not bot.config.has_section('Ask_Command'):
        bot.config.add_section('Ask_Command')
    bot.config.set('Ask_Command', 'aliases', 'Baliz')
    ask = AskCommand(bot)
    ask.dispatcher.semantic_router.decide = AsyncMock(side_effect=AssertionError('A message body must not reach the LLM'))
    manager = object.__new__(CommandManager)
    other = SimpleNamespace(should_execute=Mock(side_effect=AssertionError('Must not execute a payload command')), keywords=['test'])
    manager.commands = {'test':other,'bbs':bbs,'ask':ask}
    manager.keywords = {'help':'help text'}
    manager.monitor_channels = ['general']
    manager.logger = Mock()
    manager.record_command_stat = Mock()
    manager._viewer_bridge = lambda: None
    manager.bot = bot
    manager.normalize_command_content = lambda value: value
    manager._should_queue_command = lambda *args: (False,0)
    manager._is_channel_trigger_allowed = lambda *args: True
    manager.send_response = bot.command_manager.send_response
    manager.send_dm = bot.command_manager.send_dm
    bot.command_manager = manager
    return ask, manager, other


@pytest.mark.parametrize('body', [
    'Salut c’est un test !', 'test', 'advert', 'help', 'météo Brest ?',
    'Baliz bonjour', 'bbs r', 'Salut @[Cam] !  À demain.',
])
async def test_requested_message_body_bypasses_llm_and_other_commands(environment, body):
    _service, bbs, bot = environment
    ask, manager, other = real_assistant_manager(bot, bbs)
    await ask.dispatcher.bbs.execute(plan('send', recipient='Bob Alpha', text=''), message())
    reply = message(body)
    assert manager.check_keywords(reply) == [('ask',None)]
    await manager.execute_commands(reply)
    assert bbs._get_pending_messages_by_key(BOB)[0][2] == body
    ask.dispatcher.semantic_router.decide.assert_not_awaited()
    other.should_execute.assert_not_called()


@pytest.mark.parametrize('body', ['annule','Annule l’envoi','bbs cancel'])
async def test_requested_body_has_explicit_cancel_commands(environment, body):
    _service, bbs, bot = environment
    ask, manager, _other = real_assistant_manager(bot, bbs)
    await ask.dispatcher.bbs.execute(plan('send', recipient='Bob Alpha', text=''), message())
    reply = message(body)
    assert manager.check_keywords(reply) == [('ask',None)]
    await manager.execute_commands(reply)
    assert not bbs._get_pending_messages_by_key(BOB)
    assert not ask.dispatcher.bbs.pending
    ask.dispatcher.semantic_router.decide.assert_not_awaited()


async def test_body_capture_keeps_channel_activation_and_sender_isolation(environment):
    _service, bbs, bot = environment
    ask, _manager, _other = real_assistant_manager(bot, bbs)
    msg = message(dm=False,channel='general')
    await ask.dispatcher.bbs.execute(plan('send',recipient='Bob Alpha',text=''),msg)
    assert not ask.bbs_text_pending(message('test',dm=False,channel='general'))
    assert ask.bbs_text_pending(message('Baliz test',dm=False,channel='general'))
    assert not ask.bbs_text_pending(message('Baliz test',key=BOB2,dm=False,channel='general'))
    assert not ask.bbs_text_pending(message('Baliz test',dm=False,channel='other'))


async def test_selecting_own_number_keeps_other_choices(environment):
    service, bbs, _bot = environment
    msg = message(key=BOB)
    await service.execute(plan('send',recipient='Bob',text=''),msg)
    choices = service.pending[service.session_key(msg)].candidates
    own_number = str(next(i for i,(_name,key) in enumerate(choices,1) if key == BOB))
    other_number = str(next(i for i,(_name,key) in enumerate(choices,1) if key == BOB2))
    response = await service.execute(plan('continue',selection=own_number,text=''),msg)
    assert 'propre nœud' in response
    assert service.context(msg)['awaiting'] == 'recipient'
    assert 'Quel message' in await service.execute(plan('continue',selection=other_number,text=''),msg)
    assert not bbs._get_pending_messages_by_key(BOB)



@pytest.mark.parametrize('replace_session',[False,True])
async def test_queued_body_cannot_become_another_command_or_use_another_session(environment, replace_session):
    _service,bbs,bot = environment
    ask,manager,other = real_assistant_manager(bot,bbs)
    await ask.dispatcher.bbs.execute(plan('send',recipient='Bob Alpha',text=''),message())
    reply = message('test')
    assert manager.check_keywords(reply) == [('ask',None)]
    if replace_session:
        await ask.dispatcher.bbs.execute(plan('send',recipient='Bob Bravo',text=''),message())
    else:
        ask.dispatcher.bbs.pending[ask.dispatcher.bbs.session_key(reply)].expires = time.monotonic()-1
    await manager.execute_commands(reply)
    assert not bbs._get_pending_messages_by_key(BOB)
    assert not bbs._get_pending_messages_by_key(BOB2)
    ask.dispatcher.semantic_router.decide.assert_not_awaited()
    other.should_execute.assert_not_called()
    assert 'a expiré ou a changé' in bot.command_manager.send_response.call_args.args[1]


@pytest.mark.parametrize('question,recipient,model_text', [
    ('Laisse un message à Kam','Kam','message'),
    ('Laisse un message à Fr29-Mr One','Fr29-Mr One','un message'),
    ('Dis à Alpha bonjour','Alpha','bonjour'),
])
def test_instruction_words_or_unseparated_text_require_body_prompt(question,recipient,model_text):
    result=parse_plan(json.dumps({'route':'bbs','operation':'send',
        'args':{'recipient':recipient,'text':model_text}}),question,set(CATALOG))
    assert result.args['text']==''


@pytest.mark.parametrize('question,expected', [
    ('Dis à Alpha : Bonjour !','Bonjour !'),
    ('Dis à Alpha : « Bonjour ! »','Bonjour !'),
    ('Dis à Alpha « Bonjour ! »','Bonjour !'),
    ('Dis à Alpha "message"','message'),
    ('Dis à Alpha "Heure : midi"','Heure : midi'),
    ('Dis à Alpha : https://exemple.fr/bonjour','https://exemple.fr/bonjour'),
    ('Dis à Alpha https://exemple.fr/bonjour',''),
    ('Dis à Alpha\nBonjour !','Bonjour !'),
])
def test_initial_payload_is_a_literal_explicit_block(question,expected):
    from modules.assistant.bbs_service import explicit_message_text
    assert explicit_message_text(question,'Alpha')==expected


@pytest.mark.parametrize('choice', ['1','Bob Alpha'])
async def test_contact_choice_cannot_supply_body_even_if_model_returns_it_as_text(environment, choice):
    service,bbs,_bot=environment
    await service.execute(plan('send',recipient='Bob',text=''),message())
    response=await service.execute(plan('continue',selection=choice,text=choice),message(choice))
    assert 'Quel message' in response
    assert not bbs._get_pending_messages_by_key(BOB)
    assert not bbs._get_pending_messages_by_key(BOB2)
    assert service.context(message())['awaiting']=='text'


async def test_actual_reported_flow_separates_contact_choice_from_message_body(environment):
    _service,bbs,bot=environment
    ask,manager,_other=real_assistant_manager(bot,bbs)
    question='Laisse un message à Bob'
    # Reproduce the real model's wrong extraction of the instruction word.
    initial=parse_plan(json.dumps({'route':'bbs','operation':'send',
        'args':{'recipient':'Bob','text':'message'}}),question,set(CATALOG))
    await ask.dispatcher.bbs.execute(initial,message(question))
    response=await ask.dispatcher.bbs.execute(plan('continue',selection='2',text='2'),message('2'))
    assert 'Quel message' in response
    assert not bbs._get_pending_messages_by_key(BOB)
    assert not bbs._get_pending_messages_by_key(BOB2)
    selected=ask.dispatcher.bbs.pending[ask.dispatcher.bbs.session_key(message())].recipient[1]
    reply=message('Salut c’est un test !')
    assert manager.check_keywords(reply)==[('ask',None)]
    await manager.execute_commands(reply)
    assert bbs._get_pending_messages_by_key(selected)[0][2]=='Salut c’est un test !'
