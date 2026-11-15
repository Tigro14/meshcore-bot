"""Punctuated assistant addresses still route the original question."""
from unittest.mock import AsyncMock, patch

import pytest

from modules.commands.ask_command import AskCommand
from modules.models import MeshMessage
from tests.test_command_manager import cm_bot as cm_bot
from tests.test_command_manager import make_manager


def assistant(bot):
    bot.config.add_section('Ask_Command')
    bot.config.set('Ask_Command', 'aliases', 'Baliz')
    return AskCommand(bot)


@pytest.mark.parametrize('text', [
    'Baliz, quelle météo a coray',
    'BALIZ: quelle météo a coray',
    'Baliz : quelle météo a coray',
    'Baliz! quelle météo a coray',
    'Baliz;quelle météo a coray',
    'ask, quelle météo a coray',
    'Baliz quelle météo a coray',
])
def test_manager_recognizes_addressed_channel_question(cm_bot, text):
    ask = assistant(cm_bot)
    manager = make_manager(cm_bot, {'ask': ask})
    cm_bot.command_manager = manager
    message = MeshMessage(text, sender_id='TestUser', channel='general', is_dm=False)
    assert manager.check_keywords(message) == [('ask', None)]
    assert ask.split_trigger_and_args(message.content)[1] == 'quelle météo a coray'
    assert message.original_content == text


@pytest.mark.parametrize('text', [
    'Balizard, bonjour', 'Baliz.fr météo', 'on demande à Baliz, météo',
    '@[Autre] Baliz, météo', 'Baliz_test, météo',
])
def test_unaddressed_channel_text_is_not_changed_or_triggered(cm_bot, text):
    ask = assistant(cm_bot)
    message = MeshMessage(text, channel='general', is_dm=False)
    assert not ask.matches_keyword(message)
    assert message.content == message.original_content == text


@pytest.mark.parametrize('prefix', ['!', '/', '~'])
def test_required_prefix_is_respected_before_punctuated_address(cm_bot, prefix):
    cm_bot.config.set('Bot', 'command_prefix', prefix)
    ask = assistant(cm_bot)
    manager = make_manager(cm_bot, {'ask': ask})
    cm_bot.command_manager = manager
    assert manager.check_keywords(MeshMessage('Baliz, météo', channel='general')) == []
    message = MeshMessage(prefix+'Baliz, météo', channel='general')
    assert manager.check_keywords(message) == [('ask', None)]
    assert ask.split_trigger_and_args(message.original_content) == ('baliz', 'météo')


@pytest.mark.asyncio
@pytest.mark.parametrize('dm', [True, False])
async def test_dispatch_receives_verbatim_question_without_address(cm_bot, dm):
    ask = assistant(cm_bot)
    ask.dispatcher.answer = AsyncMock(return_value='ok')
    text = 'Baliz, quelle météo a Coray ? Et demain : pluie ?'
    message = MeshMessage(text, sender_id='TestUser', channel=None if dm else 'general', is_dm=dm)
    assert ask.matches_keyword(message)
    with patch('modules.commands.ask_command.send_answer', new_callable=AsyncMock):
        await ask.execute(message)
    ask.dispatcher.answer.assert_awaited_once_with('quelle météo a Coray ? Et demain : pluie ?', message)
    assert message.original_content == text
