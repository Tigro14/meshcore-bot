import json, sqlite3, logging, asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import pytest
from modules.assistant.catalog import parse_plan, CATALOG
from modules.assistant.router import AssistantRouter
from modules.assistant.network_plan import parameters, infer_operation
from modules.assistant.network_tools import observation_answer, trace_content
from tests.conftest import mock_message

@pytest.mark.parametrize('q,op,args',[
 ('Quels sont les 3 répéteurs les plus proches ?', 'near', {'limit':3,'role':'repeater','target':''}),
 ('statistiques des canaux','stats',{'topic':'channels','hashes':False}),
 ('statistiques des annonces avec hashes','stats',{'topic':'adverts','hashes':True}),
 ('combien de répéteurs en France depuis 24 heures ?', 'count_repeaters',{'hours':24,'country':'France','limit':5,'sort':'recent'}),
 ('liste les 10 répéteurs les plus actifs','list_repeaters',{'hours':24,'country':'','limit':10,'sort':'activity'}),
 ('Quels sont tes voisins directs ?', 'neighbors',{}),
 ('Annonce-toi sur le réseau','advert',{}),
 ('Teste le chemin vers Alpha','trace',{'path':'','target':'Alpha','roundtrip':True}),
 ('trace ab,cd','trace',{'path':'ab,cd','target':'','roundtrip':True}),
])
def test_grounded_plans(q,op,args):
 p=parse_plan(json.dumps(dict(route='mesh',operation=op,args=args)),q,set(CATALOG));assert p.operation==op;assert p.args==args

@pytest.mark.parametrize('q',['comment fonctionne advert ?', 'ne lance pas trace ab,cd', "qu’est-ce que trace ?", 'annonce-toi pas'])
def test_no_radio_from_documentation_or_negation(q):
 assert infer_operation(q) not in {'trace','advert','neighbors'}
 with pytest.raises(ValueError): parse_plan(json.dumps({'route':'mesh','operation':'advert','args':{}}),q,set(CATALOG))

def test_model_cannot_invent_target():
 with pytest.raises(ValueError):parse_plan(json.dumps({'route':'mesh','operation':'near','args':{'limit':5,'role':'repeater','target':'abcd'}}),'les répéteurs proches',set(CATALOG))

def test_queries_filter_and_count():
 c=sqlite3.connect(':memory:')
 c.execute('CREATE TABLE complete_contact_tracking (public_key TEXT,name TEXT,role TEXT,country TEXT,last_heard TEXT,advert_count INT)')
 c.execute("INSERT INTO complete_contact_tracking VALUES ('a','Alpha','repeater','France',datetime('now'),2)")
 c.execute("INSERT INTO complete_contact_tracking VALUES ('b','Bravo','repeater','France',datetime('now','-2 days'),9)")
 c.execute("INSERT INTO complete_contact_tracking VALUES ('c','Charlie','repeater','United Kingdom',datetime('now'),20)")
 a=parameters('count_repeaters','combien de répéteurs en France depuis 24 heures ?')
 assert observation_answer(c,'count_repeaters',a,logging.getLogger()).startswith('1 répéteurs')
 a=parameters('list_repeaters','liste les répéteurs en France')
 assert 'Alpha' in observation_answer(c,'list_repeaters',a,logging.getLogger())
 a['country']="France' OR 1=1 --"
 with pytest.raises(ValueError):observation_answer(c,'list_repeaters',a,logging.getLogger())

@pytest.mark.parametrize('name,flag', [('near','near_enabled'),('trace','trace_enabled'),('neighbors','command_enabled'),('advert','advert_enabled')])
def test_internal_enable_keeps_dm_and_channels(command_mock_bot,name,flag):
 import importlib
 cls=getattr(importlib.import_module('modules.commands.'+name+'_command'),name.capitalize()+'Command')
 command_mock_bot.last_advert_time=0
 cmd=cls(command_mock_bot);setattr(cmd,flag,False);cmd.cooldown_seconds=0
 cmd.is_channel_allowed=lambda m:True;cmd.requires_admin_access=lambda:False
 m=mock_message(is_dm=True)
 assert not cmd.can_execute(m);assert cmd.can_use_service(m)
 if name in {'advert','neighbors'}:assert not cmd.can_use_service(mock_message(is_dm=False))
 cmd.is_channel_allowed=lambda m:False;assert not cmd.can_use_service(m)

@pytest.mark.asyncio
async def test_neighbors_waits_for_final_capture(command_mock_bot):
 from modules.commands.neighbors_command import NeighborsCommand
 cmd=NeighborsCommand(command_mock_bot)
 service=SimpleNamespace(neighbors_enabled=True,neighbors_cycle_active=False,neighbors_config=SimpleNamespace(discover_window=1,cycle_budget=3),neighbors_cooldown_remaining=lambda:0)
 service.run_neighbors_cycle=AsyncMock(return_value={'ok':True,'discovered':2,'queried':2,'recorded':2})
 cmd._get_capture_service=lambda:service
 cmd._format_summary=lambda s:'Deux voisins directs'
 async def send(m,t,**kw):m.capture_sink.append(t);return True
 cmd.send_response=send
 m=mock_message(is_dm=True,capture_sink=[])
 await cmd.execute_service(m)
 assert m.capture_sink==['Deux voisins directs'];service.run_neighbors_cycle.assert_awaited_once()

@pytest.mark.asyncio
async def test_stats_service_uses_tigro_functions_disabled(command_mock_bot):
 from modules.commands.stats_command import StatsCommand
 cmd=StatsCommand(command_mock_bot);cmd.stats_enabled=False;cmd._get_channel_leaderboard=AsyncMock(return_value='Canaux 24h')
 cmd.send_response=AsyncMock(return_value=True)
 await cmd.execute_service(mock_message(content='stats channels'))
 cmd._get_channel_leaderboard.assert_awaited_once()

@pytest.mark.asyncio
async def test_advert_global_cooldown_and_mock_radio(command_mock_bot):
 import time
 from modules.commands.advert_command import AdvertCommand
 cmd=AdvertCommand(command_mock_bot);cmd.is_channel_allowed=lambda m:True;cmd.requires_admin_access=lambda:False
 m=mock_message(is_dm=True)
 command_mock_bot.last_advert_time=time.time()
 assert not cmd.can_use_service(m)
 command_mock_bot.last_advert_time=0
 command_mock_bot.meshcore.commands.send_advert=AsyncMock()
 cmd.send_response=AsyncMock(return_value=True)
 await cmd.execute(m)
 command_mock_bot.meshcore.commands.send_advert.assert_awaited_once_with(flood=True)
 assert not cmd.can_use_service(m)

def test_trace_resolves_target_and_rejects_unknown_path():
 c=sqlite3.connect(':memory:');c.execute('CREATE TABLE complete_contact_tracking(name TEXT,public_key TEXT,out_path TEXT,out_bytes_per_hop INT,out_path_len INT)')
 c.execute("INSERT INTO complete_contact_tracking VALUES ('Alpha','112233','aabb',1,2)")
 assert trace_content(c,{'target':'Alpha','roundtrip':True})=='tracer aa,bb,11'
 c.execute("UPDATE complete_contact_tracking SET out_path_len=-1")
 with pytest.raises(ValueError):trace_content(c,{'target':'Alpha'})
 with pytest.raises(ValueError):trace_content(c,{'target':'unknown'})

@pytest.mark.asyncio
async def test_trace_uses_mock_transport_only(command_mock_bot):
 from modules.commands.trace_command import TraceCommand
 cmd=TraceCommand(command_mock_bot);cmd.send_response=AsyncMock(return_value=True)
 cmd._format_trace_result=lambda m,r:'Trace terminée'
 cmd.update_graph_one_byte=False
 result=SimpleNamespace(success=True,path_nodes=[],flags=0)
 with patch('modules.commands.trace_command.run_trace',new=AsyncMock(return_value=result)) as trace:
  await cmd.execute(mock_message(content='tracer aa,bb',is_dm=True))
  assert trace.call_args.kwargs['path']==['aa','bb','aa']

@pytest.mark.asyncio
async def test_network_adapter_forwards_near_parameters(command_mock_bot):
 from modules.commands.near_command import NearCommand
 from modules.assistant.network_tools import answer_network
 from modules.assistant.router import Decision,Route
 near=NearCommand(command_mock_bot)
 async def run(m):
  assert m.content=='near 3 sensor abcd'
  m.capture_sink.append('Capteur à 2 km')
 near.execute=AsyncMock(side_effect=run)
 parent=SimpleNamespace(record_execution=lambda u:None)
 dispatcher=SimpleNamespace(owner=SimpleNamespace(bot=command_mock_bot,logger=logging.getLogger(),get_max_message_length=lambda m:158),_command=lambda n:near if n=='near' else parent,_allowed=lambda *a,**k:True,_rf_lock=asyncio.Lock(),_render_tool=AsyncMock(side_effect=lambda q,s,m,**kw:s))
 result=await answer_network(dispatcher,Decision(Route.MESH,'capteurs proches','test','near',{'limit':3,'role':'sensor','target':'abcd'}),mock_message())
 assert result=='Capteur à 2 km'

def test_onebyte_country_not_lost_behind_encoding():
 assert parameters('onebyte','Qui est en 1 octet en France depuis 24 heures ?')['country']=='France'
 assert parameters('trace','trace aabb')['path']=='aa,bb'
