"""Passive hop replies use RX evidence, not LLM text, guesses or a return route."""
from unittest.mock import AsyncMock, Mock

import pytest

from modules.assistant.catalog import CATALOG, parse_plan
from modules.assistant.message_hops import hops_reply, observed_hops
from modules.models import MeshMessage


@pytest.mark.parametrize('raw,expected',[(0,0),(1,1),(3,3),(0x43,3),(0x82,2),(255,None),(0xc1,None),(-1,None),(None,None),(True,None)])
def test_raw_received_dm_byte(raw,expected):
    assert observed_hops(MeshMessage('question',is_dm=True,hops=0,received_path_len=raw))==expected


@pytest.mark.parametrize('route',['DIRECT','TRANSPORT_DIRECT',2,3])
def test_direct_remaining_path_is_not_a_total_hop_count(route):
    message=MeshMessage('question',is_dm=True,hops=0,received_path_len=255,
                        routing_info={'route_type':route,'path_length':0,'path_nodes':[]})
    assert observed_hops(message) is None
    assert 'ne fournit pas' in hops_reply(message)
    message.routing_info['path_length']=2
    assert observed_hops(message) is None


@pytest.mark.parametrize('route',['FLOOD','TRANSPORT_FLOOD',0,1])
def test_correlated_flood_path_can_supply_missing_rx_count(route):
    message=MeshMessage('question',is_dm=True,hops=0,received_path_len=None,
                        routing_info={'route_type':route,'path_length':3})
    assert observed_hops(message)==3
    assert hops_reply(message)=='Ton message m’est parvenu après 3 sauts.'


def test_receive_frame_wins_over_conflicting_rf_metadata():
    message=MeshMessage('question',is_dm=True,hops=0,received_path_len=0,
                        routing_info={'route_type':'FLOOD','path_length':5})
    assert observed_hops(message)==0
    assert 'sans relais (0 saut)' in hops_reply(message)


@pytest.mark.parametrize('hops,fragment',[(0,'0 saut'),(1,'1 saut.'),(4,'4 sauts.'),(255,'pas disponible'),(None,'pas disponible')])
def test_channel_metadata_fallback(hops,fragment):
    message=MeshMessage('question',is_dm=False,hops=hops)
    assert fragment in hops_reply(message)


async def test_hop_function_is_authorized_and_sent_without_llm_rephrasing(command_mock_bot):
    from tests.test_assistant_integration import setup_bot
    commands,sent=setup_bot(command_mock_bot)
    ask=commands['ask']
    ask.dispatcher.semantic_router.decide=AsyncMock(return_value=parse_plan(
        '{"route":"path","operation":"hops","args":{}}','Combien de sauts jusqu’à toi ?',set(CATALOG)))
    commands['llm'].service.rephrase_tool_result=AsyncMock(side_effect=AssertionError('No numeric reply through LLM'))
    commands['llm'].service.answer=AsyncMock(side_effect=AssertionError('No clarification or chat'))
    commands['path'].execute=AsyncMock(side_effect=AssertionError('No name inference or active radio test'))
    message=MeshMessage('Combien de sauts jusqu’à toi ?',is_dm=True,sender_id='User',received_path_len=0x43)
    await ask.execute(message)
    assert [body for _,body in sent]==['Ton message m’est parvenu après 3 sauts.']
    commands['llm'].service.rephrase_tool_result.assert_not_awaited()
    commands['llm'].service.answer.assert_not_awaited()
    commands['path'].execute.assert_not_awaited()


async def test_hop_function_keeps_path_access_controls(command_mock_bot):
    from modules.assistant.router import Decision, Route
    from tests.test_assistant_integration import setup_bot
    commands,_sent=setup_bot(command_mock_bot)
    commands['path'].can_use_service=Mock(return_value=False)
    reply=await commands['ask'].dispatcher._dispatch(Decision(Route.PATH,'question','catalog','hops',{}),MeshMessage('question',hops=3))
    assert 'non autorisé' in reply



def test_explicit_unknown_receive_byte_cannot_be_overridden_by_rf_fallback():
    message=MeshMessage('question',is_dm=True,hops=0,received_path_len=255,
                        routing_info={'route_type':'FLOOD','path_length':3})
    assert observed_hops(message) is None
