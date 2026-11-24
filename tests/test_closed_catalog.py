import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from modules.assistant.catalog import parse_plan, CATALOG
from modules.assistant.router import AssistantRouter
from modules.assistant.dispatcher import AssistantDispatcher
from tests.conftest import mock_message

ALL=set(CATALOG)

@pytest.mark.parametrize('value',[
 {'route':'advert','operation':'send','args':{}},
 {'route':'mesh','operation':'sql','args':{'sql':'DROP TABLE contacts'}},
 {'route':'path','operation':'message','args':{'command':'reboot'}},
 {'route':'weather','operation':'forecast','args':{'location':'Brest','period':'today'}},
])
def test_reject_invalid_or_invented(value):
 with pytest.raises(ValueError): parse_plan(json.dumps(value), 'météo aujourd’hui', ALL)

def test_disabled_route_rejected():
 with pytest.raises(ValueError): parse_plan('{"route":"path","operation":"message","args":{}}','route',{'wiki'})

@pytest.mark.asyncio
async def test_model_called_for_every_question(command_mock_bot):
 d=object.__new__(AssistantDispatcher)
 d.router=AssistantRouter();d.owner=SimpleNamespace(bot=command_mock_bot,enabled_routes=ALL,logger=logging.getLogger(),route_timeout_seconds=5)
 plan=parse_plan('{"route":"path","operation":"message","args":{}}','quelle est la route ?',ALL)
 d.semantic_router=SimpleNamespace(decide=AsyncMock(return_value=plan)); d._dispatch=AsyncMock(return_value='chemin')
 assert await d.answer('quelle est la route ?',mock_message())=='chemin'
 d.semantic_router.decide.assert_awaited_once()
 assert d._dispatch.call_args[0][0].operation=='message'

def test_path_public_and_internal_are_separate(command_mock_bot):
 from tests.conftest import mock_message as make_message
 mock_message=make_message()
 from modules.commands.path_command import PathCommand
 c=PathCommand(command_mock_bot); c.path_enabled=False
 c.is_channel_allowed=lambda m: True;c.cooldown_seconds=0;c.requires_dm=False;c.requires_admin_access=lambda:False
 assert not c.can_execute(mock_message)
 assert c.can_use_service(mock_message)
 c.is_channel_allowed=lambda m: False
 assert not c.can_use_service(mock_message)

@pytest.mark.asyncio
async def test_count_uses_database_not_generated_sql(command_mock_bot, tmp_path):
 import sqlite3
 from contextlib import contextmanager
 from tests.test_assistant_integration import setup_bot
 from tests.conftest import mock_message
 commands,sent=setup_bot(command_mock_bot)
 db=tmp_path/'mesh.db'
 with sqlite3.connect(db) as c:
  c.execute('CREATE TABLE complete_contact_tracking (name TEXT, role TEXT)')
  c.executemany('INSERT INTO complete_contact_tracking VALUES (?,?)',[('A','repeater'),('B','repeater'),('C','companion')])
 @contextmanager
 def connection():
  with sqlite3.connect(db) as c: yield c
 command_mock_bot.db_manager.connection=connection
 commands['llm'].service.rephrase_tool_result=AsyncMock(return_value=None)
 commands['mesh'].service.answer=AsyncMock(side_effect=AssertionError('No generated SQL'))
 d=commands['ask'].dispatcher if hasattr(commands['ask'],'dispatcher') else commands['ask'].service
 d.semantic_router.decide=AsyncMock(return_value=parse_plan('{"route":"mesh","operation":"count_repeaters","args":{"hours":0,"country":"","limit":5,"sort":"recent"}}','combien de répéteurs ?',ALL))
 await commands['ask'].execute(mock_message(content='baliz combien de répéteurs ?'))
 assert len(sent)==1 and sent[0][1]=='2 répéteurs connus du bot.'
 commands['mesh'].service.answer.assert_not_called()

@pytest.mark.asyncio
async def test_internal_path_captures_and_preserves_sender(command_mock_bot):
 from tests.test_assistant_integration import setup_bot
 from tests.conftest import mock_message
 commands,sent=setup_bot(command_mock_bot)
 path=commands['path'];path.path_enabled=False
 commands['llm'].service.rephrase_tool_result=AsyncMock(return_value=None)
 async def execute(m):
  assert m.sender_id=='TestUser' and m.path=='aabb' and m.content=='path'
  await path.send_response(m,'Alpha puis Bravo')
 path.execute=AsyncMock(side_effect=execute)
 d=commands['ask'].dispatcher if hasattr(commands['ask'],'dispatcher') else commands['ask'].service
 d.semantic_router.decide=AsyncMock(return_value=parse_plan('{"route":"path","operation":"message","args":{}}','quelle est la route ?',ALL))
 await commands['ask'].execute(mock_message(content='baliz quelle est la route ?',path='aabb'))
 assert len(sent)==1 and sent[0][1]=='Alpha puis Bravo'

@pytest.mark.parametrize('question', ['combien de répéteurs depuis quelques jours ?', 'liste les répéteurs hier'])
def test_missing_network_arguments_rejected(question):
 with pytest.raises(ValueError):
  parse_plan('{"route":"mesh","operation":"count_repeaters","args":{}}',question,ALL)


def test_weather_rain_probability_is_not_humidity():
 source=AssistantDispatcher._expand_weather_notation('Demain: Couvert H:19°C L:14°C 13G26 🌦️38%', 'km/h')
 assert 'probabilité de pluie : 38 %' in source
 assert 'humidité' not in source
