import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from modules.assistant.catalog import parse_plan, CATALOG
from modules.assistant.dispatcher import AssistantDispatcher
from tests.conftest import mock_message

@pytest.mark.parametrize('q,route,op,args',[
 ('Baliz statistique des canaux','mesh','stats',{'topic':'channels','hashes':False}),
 ('tu en as mis du temps pour répondre','llm','chat',{}),
 ('quels sont les relais les plus sollicités','mesh','relay_connectivity',{'hours':24,'limit':3}),
 ('comment configurer un répéteur','wiki','lookup',{}),
 ('quelle est la route','path','message',{}),
 ('les relais à proximité','mesh','near',{'limit':5,'role':'repeater','target':''}),
])
@pytest.mark.asyncio
async def test_selected_plan_is_executed_without_lexical_override(q,route,op,args,command_mock_bot):
    plan=parse_plan(json.dumps(dict(route=route,operation=op,args=args)),q,set(CATALOG))
    d=object.__new__(AssistantDispatcher)
    d.owner=SimpleNamespace(bot=command_mock_bot,enabled_routes=set(CATALOG),logger=logging.getLogger(),route_timeout_seconds=2)
    d.semantic_router=SimpleNamespace(decide=AsyncMock(return_value=plan))
    d._dispatch=AsyncMock(return_value='ok')
    assert await d.answer(q,mock_message())=='ok'
    assert d._dispatch.call_args.args[0]==plan

@pytest.mark.asyncio
async def test_classifier_failure_never_dispatches(command_mock_bot):
    d=object.__new__(AssistantDispatcher)
    d.owner=SimpleNamespace(bot=command_mock_bot,enabled_routes=set(CATALOG))
    d.semantic_router=SimpleNamespace(decide=AsyncMock(return_value=None))
    d._dispatch=AsyncMock()
    assert 'service d’interprétation est momentanément indisponible' in await d.answer('tu as mis du temps',mock_message())
    d._dispatch.assert_not_called()

@pytest.mark.parametrize('args',[
 {}, {'topic':'channels','hashes':'false'}, {'topic':'other','hashes':False},
 {'topic':'channels','hashes':False,'sql':'SELECT 1'},
])
def test_reject_bad_typed_arguments(args):
    with pytest.raises(ValueError):
        parse_plan(json.dumps(dict(route='mesh',operation='stats',args=args)),'statistique des canaux',set(CATALOG))

@pytest.mark.parametrize('limit',[0,21,True])
def test_reject_invalid_limits(limit):
    with pytest.raises(ValueError):
        parse_plan(json.dumps(dict(route='mesh',operation='near',args=dict(limit=limit,role='repeater',target=''))),'proximité',set(CATALOG))


def test_catalog_examples_obey_the_contract():
    from modules.assistant.catalog import ROUTING_EXAMPLES
    for question, plan in ROUTING_EXAMPLES:
        parse_plan(json.dumps(plan), question, set(CATALOG))


def test_trace_cannot_invent_radio_path():
    with pytest.raises(ValueError):
        parse_plan(json.dumps(dict(route='mesh',operation='trace',args=dict(path='aa,bb',target='',roundtrip=True))),'teste le chemin vers Alpha',set(CATALOG))


def test_semantic_transport_uses_closed_schema_and_separate_timeout():
    from unittest.mock import patch
    from modules.assistant.semantic_router import SemanticRouter
    def read(section,key,fallback=None,value_type=None):
        return 23 if (section,key)==('Ask_Command','semantic_timeout_seconds') else fallback
    router=SemanticRouter(SimpleNamespace(logger=logging.getLogger(),get_config_value=read))
    response=SimpleNamespace(raise_for_status=lambda:None,json=lambda:{'choices':[{'message':{'content':'{"function":"llm.chat","args":{}}'}}]})
    with patch('modules.assistant.semantic_router.post_chat',return_value=response) as post:
        assert router._classify('bonjour',{'llm'}).route.value=='llm'
        payload=post.call_args.args[1]
        assert 22 < post.call_args.args[2] <= 23
        variants=payload['response_format']['json_schema']['schema']['anyOf']
        assert {v['properties']['function']['const'] for v in variants}=={'llm.chat', 'llm.clarify'}


@pytest.mark.parametrize('content', [
    '{"function":"mesh.count_nodes","args":{"hours":24,"country":""}}',
    '{"function":"llm.chat","args":{},"sql":"SELECT 1"}',
    '{"function":"llm.unknown","args":{}}',
])
def test_semantic_transport_rejects_disabled_or_unknown_calls(content):
    from unittest.mock import patch
    from modules.assistant.semantic_router import SemanticRouter
    router=SemanticRouter(SimpleNamespace(logger=logging.getLogger(),get_config_value=lambda *a, fallback=None, **k:fallback))
    response=SimpleNamespace(raise_for_status=lambda:None,json=lambda:{'choices':[{'message':{'content':content}}]})
    with patch('modules.assistant.semantic_router.post_chat',return_value=response):
        assert router._classify('bonjour',{'llm'}) is None


def test_invalid_model_plan_gets_one_repair_within_same_budget():
    from unittest.mock import patch
    from modules.assistant.semantic_router import SemanticRouter
    router=SemanticRouter(SimpleNamespace(logger=logging.getLogger(),get_config_value=lambda *a, fallback=None, **k:fallback))
    def reply(content):
        return SimpleNamespace(raise_for_status=lambda:None,json=lambda:{'choices':[{'message':{'content':content}}]})
    with patch('modules.assistant.semantic_router.post_chat',side_effect=[reply('{"function":"bad","args":{}}'),reply('{"function":"llm.chat","args":{}}')]) as post:
        assert router._classify('bonjour',{'llm'}).operation=='chat'
        assert post.call_count==2
        assert post.call_args_list[1].args[2] <= post.call_args_list[0].args[2]


def test_http_timeout_is_not_retried():
    import requests
    from unittest.mock import patch
    from modules.assistant.semantic_router import SemanticRouter
    router=SemanticRouter(SimpleNamespace(logger=logging.getLogger(),get_config_value=lambda *a, fallback=None, **k:fallback))
    with patch('modules.assistant.semantic_router.post_chat',side_effect=requests.Timeout('busy')) as post:
        assert router._classify('bonjour',{'llm'}) is None
        assert post.call_count==1


@pytest.mark.asyncio
async def test_clarification_dispatches_only_conversation_service():
    from unittest.mock import Mock
    plan = parse_plan(json.dumps(dict(route='llm', operation='clarify', args={})),
                      'statistiques de la télémétrie ?', set(CATALOG))
    service = SimpleNamespace(answer=AsyncMock(return_value='Quelles mesures souhaites-tu ?'))
    command = SimpleNamespace(service=service, can_use_service=lambda message: True,
                              record_execution=Mock())
    dispatcher = object.__new__(AssistantDispatcher)
    dispatcher.owner = SimpleNamespace(enabled_routes=set(CATALOG),
                                       get_max_message_length=lambda message: 158)
    dispatcher._command = Mock(return_value=command)
    dispatcher.record_function = Mock()
    message = SimpleNamespace(sender_id='test', is_dm=True)
    assert await dispatcher._dispatch(plan, message) == 'Quelles mesures souhaites-tu ?'
    dispatcher._command.assert_called_once_with('llm')
    service.answer.assert_awaited_once_with(plan.question, message, mode='clarify', max_length=158)
    dispatcher.record_function.assert_called_once_with('llm.clarify')

@pytest.mark.parametrize('function,topic', [
    ('bot_users', 'messages'), ('channel_traffic', 'channels'),
    ('bot_usage', 'general'), ('longest_paths', 'paths'), ('advert_counts', 'adverts'),
])
@pytest.mark.parametrize('hashes', [False, True])
def test_named_statistics_keep_existing_adapter(function, topic, hashes):
    from unittest.mock import patch
    from modules.assistant.semantic_router import SemanticRouter
    router = SemanticRouter(SimpleNamespace(
        logger=logging.getLogger(), get_config_value=lambda *a, fallback=None, **k: fallback))
    response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
        'choices': [{'message': {'content': json.dumps({
            'function': 'mesh.' + function, 'args': {'hashes': hashes}})}}]})
    with patch('modules.assistant.semantic_router.post_chat', return_value=response) as post:
        plan = router._classify('une demande', {'mesh', 'llm'})
        assert plan.operation == 'stats'
        assert plan.args == {'topic': topic, 'hashes': hashes}
        variants = post.call_args.args[1]['response_format']['json_schema']['schema']['anyOf']
        functions = {v['properties']['function']['const'] for v in variants}
        assert 'mesh.stats' not in functions
        assert 'mesh.' + function in functions


@pytest.mark.parametrize('function,args', [
    ('bot_users', {}), ('bot_users', {'hashes': 'false'}),
    ('bot_users', {'hashes': False, 'topic': 'adverts'}),
    ('stats', {'hashes': False, 'topic': 'messages'}),
])
def test_named_statistics_reject_unexposed_or_invalid_calls(function, args):
    from unittest.mock import patch
    from modules.assistant.semantic_router import SemanticRouter
    router = SemanticRouter(SimpleNamespace(
        logger=logging.getLogger(), get_config_value=lambda *a, fallback=None, **k: fallback))
    response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
        'choices': [{'message': {'content': json.dumps({
            'function': 'mesh.' + function, 'args': args})}}]})
    with patch('modules.assistant.semantic_router.post_chat', return_value=response):
        assert router._classify('une demande', {'mesh', 'llm'}) is None

@pytest.mark.parametrize('configured,llm_timeout,expected', [(None,300.0,300.0),(None,180.0,180.0),(90.0,300.0,90.0),(500.0,300.0,300.0)])
def test_semantic_deadline_inherits_llm_timeout(configured, llm_timeout, expected):
    import logging
    from types import SimpleNamespace
    from modules.assistant.semantic_router import SemanticRouter
    def read(section,key,fallback=None,value_type='str'):
        if (section,key) == ('Llm_Command','timeout_seconds'):
            return llm_timeout
        if key == 'semantic_timeout_seconds' and configured is not None:
            return configured
        return fallback
    router = SemanticRouter(SimpleNamespace(logger=logging.getLogger(),get_config_value=read))
    assert router.timeout_seconds == expected

@pytest.mark.parametrize('question,place', [
    ('meteo st brieuc','Saint-Brieuc'),
    ('meteo chateauneuf du faou','Châteauneuf-du-Faou'),
    ('météo ste anne d auray','Sainte-Anne-d’Auray'),
])
def test_weather_accepts_equivalent_place_spelling(question,place):
    plan = parse_plan(json.dumps({'route':'weather','operation':'forecast','args':{'location':place,'period':'today'}}),question,{'weather'})
    assert plan.args['location'] == place

@pytest.mark.parametrize('place', ['Paris','Saint-Brieuc France','Faou'])
def test_weather_does_not_accept_invented_or_partial_place(place):
    with pytest.raises(ValueError):
        parse_plan(json.dumps({'route':'weather','operation':'forecast','args':{'location':place,'period':'today'}}),'meteo st brieuc',{'weather'})

@pytest.mark.parametrize('second,expected', [
    ({'function':'mesh.summary','args':{}},None),
    ({'function':'weather.forecast','args':{'location':'','period':'today'}},None),
    ({'function':'weather.forecast','args':{'location':'st brieuc','period':'today'}},'forecast'),
])
def test_weather_argument_repair_cannot_switch_to_network(second,expected):
    from unittest.mock import patch
    from modules.assistant.semantic_router import SemanticRouter
    router=SemanticRouter(SimpleNamespace(logger=logging.getLogger(),get_config_value=lambda *a,fallback=None,**k:fallback))
    def reply(value):
        return SimpleNamespace(raise_for_status=lambda:None,json=lambda:{'choices':[{'message':{'content':json.dumps(value)}}]})
    first={'function':'weather.forecast','args':{'location':'Saint-Brieuc France','period':'today'}}
    with patch('modules.assistant.semantic_router.post_chat',side_effect=[reply(first),reply(second)]) as post:
        result=router._classify('meteo st brieuc',{'weather','mesh'})
        assert (result.operation if result else None)==expected
        variants=post.call_args.args[1]['response_format']['json_schema']['schema']['anyOf']
        assert [v['properties']['function']['const'] for v in variants]==['weather.forecast']


def test_weather_repair_spans_do_not_replace_requested_location():
    from modules.assistant.catalog import grounded_place
    assert not grounded_place('Brest','meteo st brieuc')
    assert not grounded_place('chateauneuf-du-fau','meteo chateauneuf du faou')
