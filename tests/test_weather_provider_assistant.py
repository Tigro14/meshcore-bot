"""Weather source comes from the initialized backend, never a generated guess."""
import json
from unittest.mock import AsyncMock, Mock, patch

import pytest

from modules.assistant.catalog import CATALOG, parse_plan
from modules.assistant.router import Decision, Route
from modules.commands.wx_command import WxCommand
from tests.conftest import mock_message
from tests.test_assistant_integration import setup_bot


@pytest.mark.parametrize('provider,fallback,expected',[
    ('openmeteo',False,'Open-Meteo'),
    ('openmeteo',True,'Open-Meteo'),
    ('noaa',False,'NOAA / National Weather Service'),
    ('noaa',True,'NOAA/NWS, avec Open-Meteo'),
])
def test_describes_initialized_weather_backend(command_mock_bot,provider,fallback,expected):
    command_mock_bot.config.add_section('Weather')
    command_mock_bot.config.set('Weather','weather_provider',provider)
    command_mock_bot.config.set('Weather','openmeteo_fallback',str(fallback))
    command_mock_bot.config.set('Weather','custom.wxsim.default','https://private-user:private-password@private-source.invalid/feed')
    with patch('requests.get',side_effect=AssertionError('Source query must not call weather APIs')):
        command=WxCommand(command_mock_bot)
        assert expected in command.provider_description()
        assert 'private-' not in command.provider_description()


async def test_provider_answer_is_authorized_and_not_rephrased(command_mock_bot):
    commands,sent=setup_bot(command_mock_bot)
    plan=Decision(Route.WEATHER,'Quel fournisseur te fournit la météo ?','catalog','provider',{})
    commands['ask'].dispatcher.semantic_router.decide=AsyncMock(return_value=plan)
    commands['wx'].execute=AsyncMock(side_effect=AssertionError('No forecast execution'))
    commands['llm'].service.answer=AsyncMock(side_effect=AssertionError('No telemetry clarification'))
    commands['llm'].service.rephrase_tool_result=AsyncMock(side_effect=AssertionError('Do not invent a provider'))
    message=mock_message('Quel fournisseur te fournit la météo ?',is_dm=True)
    await commands['ask'].execute(message)
    assert [text for _,text in sent]==['Mon fournisseur principal pour les prévisions météo est Open-Meteo.']
    commands['wx'].execute.assert_not_awaited()
    commands['llm'].service.answer.assert_not_awaited()
    commands['llm'].service.rephrase_tool_result.assert_not_awaited()


async def test_provider_service_keeps_access_controls(command_mock_bot):
    commands,_sent=setup_bot(command_mock_bot)
    commands['wx'].can_use_service=Mock(return_value=False)
    reply=await commands['ask'].dispatcher._dispatch(
        Decision(Route.WEATHER,'source météo','catalog','provider',{}),mock_message())
    assert 'ne peux pas consulter' in reply


@pytest.mark.parametrize('args',[{'location':'Brest'},{'provider':'invented'},{'api_key':'secret'}])
def test_provider_has_no_model_supplied_arguments(args):
    with pytest.raises(ValueError):
        parse_plan(json.dumps({'route':'weather','operation':'provider','args':args}),'source météo',set(CATALOG))


def test_provider_operation_accepts_empty_args_without_location():
    decision=parse_plan('{"route":"weather","operation":"provider","args":{}}',
                        'Quel fournisseur te fournit la météo ?',set(CATALOG))
    assert decision.operation=='provider'


def test_forecast_still_requires_grounded_location_and_period():
    with pytest.raises(ValueError):
        parse_plan('{"route":"weather","operation":"forecast","args":{}}','météo Brest',set(CATALOG))
    decision=parse_plan('{"route":"weather","operation":"forecast","args":{"location":"Brest","period":"today"}}',
                        'météo Brest',set(CATALOG))
    assert decision.args['location']=='Brest'
