"""A source registry is consulted only for explicit provenance operations."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from modules.assistant.catalog import CATALOG, parse_plan
from modules.assistant.provenance import SOURCES, TOPICS, answer_source
from modules.assistant.router import Decision, Route
from modules.assistant.semantic_router import SemanticRouter
from modules.models import MeshMessage
from tests.conftest import mock_message
from tests.test_assistant_integration import setup_bot


@pytest.mark.parametrize('topic',TOPICS)
def test_topic_contract(topic):
    decision=parse_plan(json.dumps({'route':'info','operation':'provenance','args':{'topic':topic}}),
                        'Quelle est la source ?',set(CATALOG))
    assert decision.args=={'topic':topic}


@pytest.mark.parametrize('args',[{}, {'topic':'private_config'}, {'topic':'weather'},
                              {'topic':'network','sql':'SELECT secret'}, {'topic':None}])
def test_provenance_is_a_closed_metadata_catalog(args):
    with pytest.raises(ValueError):
        parse_plan(json.dumps({'route':'info','operation':'provenance','args':args}),
                   'source de tes données',set(CATALOG))


@pytest.mark.parametrize('topic',TOPICS)
def test_source_answers_fit_a_short_radio_message(topic):
    source=SOURCES[topic]
    assert len(source.text.encode('utf-8'))<=142


async def test_requested_source_is_sent_directly_without_llm_rephrasing(command_mock_bot):
    commands,sent=setup_bot(command_mock_bot)
    commands['ask'].dispatcher.semantic_router.decide=AsyncMock(return_value=Decision(
        Route.INFO,'Quelle est la source de tes statistiques réseau ?','catalog','provenance',{'topic':'network'}))
    commands['llm'].service.answer=AsyncMock(side_effect=AssertionError('No source generation'))
    commands['llm'].service.rephrase_tool_result=AsyncMock(side_effect=AssertionError('No source generation'))
    await commands['ask'].execute(mock_message('Quelle est la source de tes statistiques réseau ?',is_dm=True))
    assert [body for _,body in sent]==[SOURCES['network'].text]
    commands['llm'].service.answer.assert_not_awaited()
    commands['llm'].service.rephrase_tool_result.assert_not_awaited()


async def test_normal_hop_answer_has_no_source_appendix(command_mock_bot):
    commands,sent=setup_bot(command_mock_bot)
    commands['ask'].dispatcher.semantic_router.decide=AsyncMock(return_value=Decision(
        Route.PATH,'Combien de sauts jusqu’à toi ?','catalog','hops',{}))
    with patch('modules.assistant.provenance.answer_source',side_effect=AssertionError('No automatic source')):
        await commands['ask'].execute(MeshMessage('Combien de sauts jusqu’à toi ?',is_dm=True,
                                     sender_id='User',received_path_len=3))
    assert [body for _,body in sent]==['Ton message m’est parvenu après 3 sauts.']


@pytest.mark.parametrize('route,operation,args,method',[
    (Route.WEATHER,'forecast',{'location':'Brest','period':'today'},'_weather_tool'),
    (Route.TEST,'receive',{},'_rf_tool'),
])
async def test_ordinary_tools_keep_their_exact_reply(command_mock_bot,route,operation,args,method):
    commands,_sent=setup_bot(command_mock_bot)
    dispatcher=commands['ask'].dispatcher
    setattr(dispatcher,method,AsyncMock(return_value='Réponse courte inchangée.'))
    with patch('modules.assistant.provenance.answer_source',side_effect=AssertionError('No automatic source')):
        result=await dispatcher._dispatch(Decision(route,'question','catalog',operation,args),mock_message())
    assert result=='Réponse courte inchangée.'


async def test_normal_network_answer_keeps_its_exact_reply(command_mock_bot):
    commands,_sent=setup_bot(command_mock_bot)
    with patch('modules.assistant.provenance.answer_source',side_effect=AssertionError('No automatic source')):
        with patch('modules.assistant.network_tools.answer_network',new=AsyncMock(return_value='656 nœuds.')):
            result=await commands['ask'].dispatcher._dispatch(
                Decision(Route.MESH,'Résumé du réseau','catalog','summary',{}),mock_message())
    assert result=='656 nœuds.'


def test_descriptions_are_not_injected_into_classification_prompts():
    router=SemanticRouter(SimpleNamespace(logger=Mock(),get_config_value=lambda *a,fallback=None,**k:fallback))
    response=SimpleNamespace(raise_for_status=lambda:None,json=lambda:{'choices':[{'message':{
        'content':'{"function":"weather.forecast","args":{"location":"Brest","period":"today"}}'}}]})
    with patch('modules.assistant.semantic_router.post_chat',return_value=response) as post:
        router._classify('météo Brest',set(CATALOG))
    payload=json.dumps(post.call_args.args[1],ensure_ascii=False)
    for source in SOURCES.values():
        if source.text: assert source.text not in payload


def test_permission_denial_does_not_read_private_data():
    dispatcher=SimpleNamespace(_command=Mock(return_value=Mock()),_allowed=Mock(return_value=False),record_function=Mock())
    reply=answer_source(dispatcher,'network',mock_message())
    assert 'ne peux pas consulter' in reply
    dispatcher.record_function.assert_not_called()


def test_inactive_documentary_service_is_reported_as_inactive():
    command=SimpleNamespace(service=SimpleNamespace(wiki_rag=None),record_execution=Mock())
    dispatcher=SimpleNamespace(_command=lambda _:command,_allowed=lambda *a,**k:True,record_function=Mock())
    assert 'pas active actuellement' in answer_source(dispatcher,'wiki',mock_message())


def test_model_question_reads_the_initialized_model_without_exposing_endpoints():
    command=SimpleNamespace(service=SimpleNamespace(model='qwen3.5:2b-q4_K_M',endpoint='private-endpoint'))
    dispatcher=SimpleNamespace(_command=lambda _:command,_allowed=lambda *a,**k:True,record_function=Mock())
    reply=answer_source(dispatcher,'assistant',mock_message())
    assert reply=='Le modèle configuré pour Baliz est qwen3.5:2b-q4_K_M.'
    assert 'private-endpoint' not in reply


@pytest.mark.parametrize('model',[None,'', 'x'*200, 'model\nprivate'])
def test_unavailable_or_invalid_model_names_are_not_invented(model):
    command=SimpleNamespace(service=SimpleNamespace(model=model))
    dispatcher=SimpleNamespace(_command=lambda _:command,_allowed=lambda *a,**k:True,record_function=Mock())
    assert answer_source(dispatcher,'assistant',mock_message())==SOURCES['assistant'].text


@pytest.mark.parametrize('route,operation,reply',[(Route.LLM,'chat','Bonjour !'),(Route.WIKI,'lookup','Réglage documenté.')])
async def test_conversation_and_documentation_get_no_provenance_context(command_mock_bot,route,operation,reply):
    commands,_sent=setup_bot(command_mock_bot)
    commands['llm'].service.answer=AsyncMock(return_value=reply)
    with patch('modules.assistant.provenance.answer_source',side_effect=AssertionError('No automatic source')):
        result=await commands['ask'].dispatcher._dispatch(Decision(route,'question','catalog',operation,{}),mock_message())
    assert result==reply
    assert commands['llm'].service.answer.call_args.args[0]=='question'
    assert not any(key in commands['llm'].service.answer.call_args.kwargs for key in ['sources','provenance','context'])
