import configparser
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from modules.assistant.dispatcher import AssistantDispatcher
from modules.assistant.response import clarification_mention
from modules.assistant.router import Decision, Route
from modules.commands.ask_command import AskCommand
from modules.commands.base_command import BaseCommand
from modules.models import MeshMessage


def assistant(mode='also'):
    command = object.__new__(AskCommand)
    config = configparser.ConfigParser()
    config.read_dict({'Bot': {'respond_to_mentions': mode}})
    command.bot = SimpleNamespace(config=config)
    command._get_bot_name = lambda: '[BOT] Baliz'
    command._command_prefixes = []
    command._require_command_prefix = True
    command.keywords = ['ask', 'Baliz']
    return command


@pytest.mark.parametrize('text', [
    '@[[BOT] Baliz] quel est le meilleur répéteur ?',
    'quel est le meilleur répéteur ? @[[BOT] Baliz]',
    '@[[bot] baliz] Baliz quel est le meilleur répéteur ?',
])
@pytest.mark.parametrize('stripped', [True, False])
def test_native_mention_invokes_assistant_without_losing_question(text, stripped):
    command = assistant()
    message = MeshMessage(content=text)
    if stripped:
        import re
        message.content = re.sub(r'@\[\[bot\] baliz\]', '', text, flags=re.I).strip()
    assert command.matches_keyword(message)
    assert command.split_trigger_and_args(message.content)[1] == 'quel est le meilleur répéteur ?'
    assert message.original_content == text
    # Repeated eligibility checks must not stack trigger words.
    assert command.matches_keyword(message)
    assert message.content == 'ask quel est le meilleur répéteur ?'


@pytest.mark.parametrize('text,mode', [('@[Autre] bonjour','also'),('@[[BOT] Baliz] bonjour','false')])
def test_other_mentions_or_disabled_mentions_do_not_invoke_assistant(text,mode):
    with patch.object(BaseCommand,'matches_keyword',return_value=False) as fallback:
        assert not assistant(mode).matches_keyword(MeshMessage(content=text))
        fallback.assert_called_once()


@pytest.mark.parametrize('name', ['Cam', 'Cam 🏠', '[FR29] Cam'])
def test_sender_mention_uses_exact_name(name):
    assert clarification_mention(MeshMessage(content='',sender_id=name),158) == f'@[{name}] '


@pytest.mark.parametrize('name', ['Unknown', '', 'Cam] @[Autre', '{bot_name}', 'Cam\nAutre', 'é'*100])
def test_unsafe_missing_or_oversized_names_are_not_mentioned(name):
    assert clarification_mention(MeshMessage(content='',sender_id=name),158) == ''


@pytest.mark.asyncio
@pytest.mark.parametrize('is_dm', [True,False])
async def test_clarification_mentions_sender_only_in_channel_and_reserves_bytes(is_dm):
    dispatcher = object.__new__(AssistantDispatcher)
    dispatcher.owner = SimpleNamespace(enabled_routes={'llm'},get_max_message_length=lambda message:158)
    command = SimpleNamespace(service=SimpleNamespace(answer=AsyncMock(return_value='Quel critère ?')),record_execution=Mock())
    dispatcher._command = lambda name: command
    dispatcher._allowed = lambda *args,**kwargs: True
    dispatcher.record_function = Mock()
    message = MeshMessage(content='ask meilleur ?',sender_id='Cam 🏠',is_dm=is_dm)
    decision = Decision(Route.LLM,'meilleur ?','catalog','clarify',{})
    result = await dispatcher._dispatch(decision,message)
    prefix = '' if is_dm else '@[Cam 🏠] '
    assert result == prefix+'Quel critère ?'
    assert command.service.answer.call_args.kwargs['max_length'] == 158-len(prefix.encode())


def test_manager_routes_mention_to_assistant_once_even_for_command_body():
    from modules.command_manager import CommandManager
    command = assistant()
    command.ask_enabled = True
    command.should_execute = command.matches_keyword
    command.can_execute = lambda message: True
    command.requires_internet = False
    command.get_response_format = lambda: None
    ping = SimpleNamespace(should_execute=lambda message: message.content == 'ping', keywords=['ping'])
    manager = object.__new__(CommandManager)
    manager.commands = {'ping':ping,'ask':command}
    manager.keywords = {}
    manager.normalize_command_content = lambda text: text
    manager._should_queue_command = lambda *args: (False,0)
    manager._is_channel_trigger_allowed = lambda *args: True
    message = MeshMessage(content='@[[BOT] Baliz] ping',is_dm=True)
    assert manager.check_keywords(message) == [('ask',None)]
    assert message.content == 'ask ping'


@pytest.mark.parametrize('is_dm,enabled,allowed,expected', [
    (True,True,True,[('ask',None)]),
    (False,True,True,[]),
    (True,False,True,[]),
    (True,True,False,[]),
])
def test_bare_question_only_triggers_in_private(is_dm,enabled,allowed,expected):
    from modules.command_manager import CommandManager
    command = assistant()
    command.ask_enabled = enabled
    command.should_execute = command.matches_keyword
    command.can_execute = lambda message: enabled and allowed
    command.requires_internet = False
    command.get_response_format = lambda: None
    manager = object.__new__(CommandManager)
    manager.commands = {'ask':command}
    manager.keywords = {}
    manager.normalize_command_content = lambda text: text
    manager._should_queue_command = lambda *args: (False,0)
    manager._is_channel_trigger_allowed = lambda *args: True
    message = MeshMessage(content='le plus de liens',is_dm=is_dm)
    assert manager.check_keywords(message) == expected
    assert message.original_content == 'le plus de liens'
    if expected:
        assert command.split_trigger_and_args(message.content)[1] == 'le plus de liens'


def test_private_existing_command_keeps_priority_even_when_denied():
    from modules.command_manager import CommandManager
    command = assistant()
    command.ask_enabled = True
    command.should_execute = command.matches_keyword
    manager = object.__new__(CommandManager)
    manager.commands = {'ping':SimpleNamespace(should_execute=lambda m:m.content=='ping',can_execute=lambda m:False), 'ask':command}
    manager.keywords = {}
    manager.normalize_command_content = lambda text:text
    manager._should_queue_command = lambda *args:(False,0)
    message = MeshMessage(content='ping',is_dm=True)
    assert manager.check_keywords(message) == []
    assert message.content == 'ping'
