import sqlite3
from types import SimpleNamespace
from contextlib import contextmanager
from unittest.mock import AsyncMock
import pytest
from modules.near_location import first_hop, resolve_origin


def message(**kwargs):
    return SimpleNamespace(**dict(dict(sender_pubkey='sender', sender_id='User', path='', routing_info={}), **kwargs))


@pytest.fixture
def db():
    c = sqlite3.connect(':memory:')
    c.execute('CREATE TABLE complete_contact_tracking (public_key TEXT, name TEXT, latitude REAL, longitude REAL, role TEXT)')
    c.executemany('INSERT INTO complete_contact_tracking VALUES (?,?,?,?,?)', [
        ('aabbcc11','First',48,-4,'repeater'),('ddeeff11','Last',49,-3,'repeater'),
        ('11223344','Nearby',48.01,-4,'repeater')])
    return c


@pytest.mark.parametrize('width,nodes', [(1,['aa','dd']),(2,['aabb','ddee']),(3,['aabbcc','ddeeff'])])
def test_first_not_last(db,width,nodes):
    m=message(routing_info={'path_nodes':nodes,'bytes_per_hop':width,'path_length':2})
    origin,error=resolve_origin(db,m)
    assert not error and origin.public_key=='aabbcc11' and 'approx.' in origin.label
    assert first_hop(message(routing_info={'path_hex':''.join(nodes),'bytes_per_hop':width}))==nodes[0]


@pytest.mark.parametrize('path', ['aa,dd (2 hops)','aa,dd via ROUTE_TYPE_FLOOD'])
def test_legacy(path):
    assert first_hop(message(path=path))=='aa'


@pytest.mark.parametrize('routing', [dict(path_length=0,path_nodes=['aa']),dict(path_nodes=['aa','zz']),dict(path_nodes=['aa'],bytes_per_hop=4),dict(path_hex='aabbdd'),dict(path_nodes=['aa','dddd'])])
def test_invalid_metadata(routing):
    assert first_hop(message(routing_info=routing)) is None


def test_sender_gps_priority(db):
    db.execute("INSERT INTO complete_contact_tracking VALUES ('sender','User',0,10,'companion')")
    origin,error=resolve_origin(db,message(path='aa,dd'))
    assert not error and origin.latitude==0 and not origin.label
    origin,error=resolve_origin(db,message(sender_pubkey='',sender_id='sender'))
    assert origin.longitude==10


def test_collision_even_without_gps(db):
    db.execute("INSERT INTO complete_contact_tracking VALUES ('aa9999','Collision',NULL,NULL,'repeater')")
    origin,error=resolve_origin(db,message(path='aa,dd'))
    assert origin is None and 'plusieurs' in error


def test_unknown_first_never_uses_last(db):
    origin,error=resolve_origin(db,message(path='ff,dd'))
    assert origin is None and 'inconnu' in error


def test_public_key_not_duplicate_name(db):
    db.execute("INSERT INTO complete_contact_tracking VALUES ('other','User',45,2,'companion')")
    origin,error=resolve_origin(db,message(path='aa,dd'))
    assert origin.public_key=='aabbcc11'


@pytest.mark.asyncio
async def test_near_distances_and_budget(db,command_mock_bot):
    from modules.commands.near_command import NearCommand
    from tests.conftest import mock_message
    @contextmanager
    def connection():
        yield db
    command_mock_bot.db_manager=SimpleNamespace(connection=connection)
    cmd=NearCommand(command_mock_bot)
    cmd.send_response=AsyncMock(return_value=True)
    cmd.get_max_message_length=lambda m:158
    m=mock_message(content='near 1 repeater')
    m.sender_pubkey='sender';m.routing_info={'path_nodes':['aa','dd'],'bytes_per_hop':1};m.path=''
    await cmd.execute(m)
    text=cmd.send_response.call_args.args[1]
    assert 'Repère : 1er relais First (approx.).' in text
    assert 'Nearby: 1.1 km' in text and 'Last:' not in text
    assert len(text.encode())<=158
