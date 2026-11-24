"""Count all observed identities, preserve role-independent and temporal scope."""
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from modules.assistant.catalog import CATALOG, parse_plan
from modules.assistant.network_tools import count_nodes, answer_network


def database():
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE complete_contact_tracking(public_key TEXT, role TEXT, country TEXT, last_heard TEXT)')
    for key, role, country, age in [
        ('a','repeater','France','-2 hours'),
        ('b','companion','France','-3 hours'),
        ('c','roomserver','United Kingdom','-4 hours'),
        ('d','sensor','France','-25 hours'),
        ('a','repeater','France','-1 hours'),
        ('e','repeater','France','+1 hours'),
    ]:
        db.execute("INSERT INTO complete_contact_tracking VALUES(?,?,?,datetime('now','localtime',?))", (key,role,country,age))
    return db


def test_counts_all_roles_distinct_recent_identities():
    with database() as db:
        assert count_nodes(db,dict(hours=24,country='')).startswith('3 nœuds')
        assert count_nodes(db,dict(hours=24,country='France')).startswith('2 nœuds')
        assert count_nodes(db,dict(hours=0,country='')).startswith('5 nœuds')
        assert 'annonces' in count_nodes(db,dict(hours=24,country=''))
        assert '24 h' in count_nodes(db,dict(hours=24,country=''))


def test_empty_observations_are_zero():
    with database() as db:
        db.execute('DELETE FROM complete_contact_tracking')
        assert count_nodes(db,dict(hours=24,country='')).startswith('0 nœuds')


@pytest.mark.parametrize('args', [dict(hours=-1,country=''),dict(hours=True,country=''),dict(hours=24,country='',sql='DROP TABLE x')])
def test_count_rejects_invalid_model_arguments(args):
    with pytest.raises(ValueError):
        parse_plan(json.dumps(dict(route='mesh',operation='count_nodes',args=args)), 'nœuds actifs', set(CATALOG))


@pytest.mark.asyncio
async def test_count_result_passes_to_llm_without_radio():
    db=database()
    parent=SimpleNamespace(record_execution=lambda _:None)
    dispatcher=SimpleNamespace(
        _command=lambda _:parent, _allowed=lambda *a,**k:True,
        owner=SimpleNamespace(bot=SimpleNamespace(db_manager=SimpleNamespace(connection=lambda:db)),
                              logger=None,get_max_message_length=lambda _:220),
        _render_tool=AsyncMock(return_value='Baliz a observé 3 nœuds sur 24 h.'))
    plan=parse_plan(json.dumps(dict(route='mesh',operation='count_nodes',args=dict(hours=24,country=''))),'combien de nœuds actifs ?',set(CATALOG))
    assert await answer_network(dispatcher,plan,SimpleNamespace(sender_id='sender'))=='Baliz a observé 3 nœuds sur 24 h.'
    assert dispatcher._render_tool.call_args.args[1].startswith('3 nœuds')
