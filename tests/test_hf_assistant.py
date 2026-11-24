import json
from unittest.mock import AsyncMock, patch

import pytest

from modules.assistant.catalog import CATALOG, parse_plan
from modules.assistant.dispatcher import AssistantDispatcher
from modules.commands.base_command import BaseCommand
from modules.commands.hfcond_command import HfcondCommand
from tests.conftest import mock_message


def test_hf_catalog():
    plan = parse_plan(json.dumps({'route': 'mesh', 'operation': 'hfcond', 'args': {}}),
                      'conditions HF', set(CATALOG))
    assert plan.operation == 'hfcond'


@pytest.mark.asyncio
async def test_hf_hidden_service_and_capture(command_mock_bot):
    command_mock_bot.config.add_section('Hfcond_Command')
    command_mock_bot.config.set('Hfcond_Command', 'enabled', 'false')
    command = HfcondCommand(command_mock_bot)
    message = mock_message('hfcond')
    with patch.object(BaseCommand, 'can_execute', return_value=True):
        assert not command.can_execute(message)
        assert command.can_use_service(message)
    command.send_response = AsyncMock(return_value=True)
    with patch('modules.commands.hfcond_command.hf_band_conditions', return_value='d20m=Good'):
        await command.execute(message)
    assert 'd20m=Good' in command.send_response.call_args.args[1]


def test_weather_fallback_complete_and_clean():
    source = "En Bretagne: Aujourd'hui: Partiellement nuageux 17°C ↘️SE5G13 60%RH | H:20°C L:12°C"
    expanded = AssistantDispatcher._expand_weather_notation(source, 'km/h')
    answer = AssistantDispatcher._weather_fallback(expanded, 158)
    assert '↘' not in answer and '…' not in answer
    assert 'température actuelle' not in answer
    assert all(value in answer for value in ['17°C', '20°C', '12°C', '5', '13', '60'])
    assert len(answer.encode()) <= 158


@pytest.mark.asyncio
async def test_hf_adapter_calls_llm(command_mock_bot):
    import asyncio
    import logging
    from types import SimpleNamespace

    from modules.assistant.network_tools import answer_network
    from modules.assistant.router import Decision, Route

    command = HfcondCommand(command_mock_bot)
    async def capture(message):
        message.capture_sink.append('d20m=Good n20m=Fair')
    command.execute = AsyncMock(side_effect=capture)
    command.record_execution = lambda sender: None
    parent = SimpleNamespace(record_execution=lambda sender: None)
    renderer = AsyncMock(return_value='HF : 20 m bon le jour, moyen la nuit.')
    dispatcher = SimpleNamespace(
        owner=SimpleNamespace(bot=command_mock_bot, logger=logging.getLogger()),
        _command=lambda name: command if name == 'hfcond' else parent,
        _allowed=lambda *a, **kw: True,
        _rf_lock=asyncio.Lock(), _render_tool=renderer,
    )
    result = await answer_network(dispatcher, Decision(Route.MESH, 'conditions HF', 'catalog', 'hfcond', {}), mock_message())
    assert result == renderer.return_value
    command.execute.assert_awaited_once()
    renderer.assert_awaited_once()
    assert 'HF globales' in renderer.call_args.kwargs['context']


@pytest.mark.parametrize('question,route,operation', [
    ('condition radio', 'mesh', 'hfcond'),
    ('tropo', 'mesh', 'hfcond'),
    ('quels sont les canaux utilisés en Bretagne ?', 'wiki', 'lookup'),
    ('statistique des canaux', 'mesh', 'stats'),
])
def test_catalog_examples_keep_documentation_and_radio_distinct(question, route, operation):
    from modules.assistant.catalog import ROUTING_EXAMPLES
    example = dict(ROUTING_EXAMPLES)[question]
    plan = parse_plan(json.dumps(example), question, set(CATALOG))
    assert (plan.route.value, plan.operation) == (route, operation)
