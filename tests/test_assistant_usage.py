import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from modules.assistant.dispatcher import AssistantDispatcher
from modules.assistant.router import Decision, Route
from modules.assistant.usage import top_functions
from modules.db_migrations import _m0030_assistant_function_usage


def test_usage_windows_and_ranking():
    conn = sqlite3.connect(':memory:')
    _m0030_assistant_function_usage(conn.cursor())
    _m0030_assistant_function_usage(conn.cursor())
    now = 4000000
    conn.executemany('INSERT INTO assistant_function_usage VALUES (?, ?)', [
        (now - 10, 'weather.forecast'), (now - 20, 'weather.forecast'),
        (now - 30, 'mesh.stats.channels'), (now - 90000, 'wiki.lookup'),
        (now - 700000, 'llm.chat'), (now - 3000000, 'test.receive'),
    ])
    assert [sum(x['count'] for x in top_functions(conn.cursor(), w, now))
            for w in ['24h', '7d', '30d', 'all']] == [3, 4, 5, 6]
    rows = top_functions(conn.cursor(), '24h', now)
    assert rows[0]['command'] == 'Météo'
    assert rows[1]['command'] == 'Statistiques : canaux'


def test_dispatch_counts_authorized_call_once():
    command = Mock()
    command.can_use_service.return_value = True
    command.service.answer = AsyncMock(return_value='Bonjour')
    db = Mock()
    owner = Mock()
    owner.bot.db_manager = db
    owner.bot.command_manager.commands = {'llm': command}
    owner.get_config_value.side_effect = lambda *a, **kw: kw.get("fallback")
    owner.enabled_routes = {'llm'}
    dispatcher = AssistantDispatcher(owner)
    decision = Decision(Route.LLM, 'bonjour', 'test')
    message = SimpleNamespace(sender_id='sender')
    asyncio.run(dispatcher._dispatch(decision, message))
    assert db.execute_update.call_count == 1
    assert db.execute_update.call_args.args[1][1] == 'llm.chat'
    db.reset_mock()
    command.can_use_service.return_value = False
    asyncio.run(dispatcher._dispatch(decision, message))
    db.execute_update.assert_not_called()
    command.can_use_service.return_value = True
    owner.enabled_routes = set()
    asyncio.run(dispatcher._dispatch(decision, message))
    db.execute_update.assert_not_called()
