"""Preserve Baliz defaults while adapting optional upstream improvements."""
import configparser
import errno
import logging
from unittest.mock import Mock, patch

import requests

from modules.assistant.llm_backend import LlmBackend
from modules.io_health import IOHealthMonitor


def backend(**values):
    def read(section, key, fallback=None, **kwargs):
        return values.get(key, fallback)
    return LlmBackend(read, logging.getLogger(__name__), 'http://local/v1/chat/completions')


def response(status=200):
    r = Mock(status_code=status)
    if status != 200:
        r.raise_for_status.side_effect = requests.HTTPError('unavailable')
    return r


def test_no_primary_keeps_local_and_does_not_probe():
    b = backend()
    with patch('modules.assistant.llm_backend.requests.get') as get:
        assert b.resolve() == 'http://local/v1/chat/completions'
    get.assert_not_called()


def test_health_probe_is_cached_and_auth_only_goes_to_primary():
    b = backend(primary_endpoint='https://primary/v1/chat/completions', api_key='private-test')
    with patch('modules.assistant.llm_backend.requests.get', return_value=response()) as get:
        assert b.resolve() == b.primary
        assert b.resolve() == b.primary
    get.assert_called_once()
    assert b.headers(b.fallback) == {}
    assert b.headers(b.primary) == {'Authorization': 'Bearer private-test'}


def test_failed_primary_retries_fallback_without_leaking_credentials():
    b = backend(primary_endpoint='https://primary/v1/chat/completions', api_key='private-test')
    with patch('modules.assistant.llm_backend.requests.get', return_value=response()), patch('modules.assistant.llm_backend.requests.post', side_effect=[response(503), response()]) as post:
        assert b.post_chat({'messages': []}, 10).status_code == 200
    assert post.call_args_list[0].kwargs['headers']['Authorization'] == 'Bearer private-test'
    assert post.call_args_list[1].kwargs['headers'] == {}
    assert b.active_endpoint == b.fallback
    assert post.call_args_list[1].kwargs['timeout'] <= post.call_args_list[0].kwargs['timeout']


def test_unconfigured_watchdog_never_requests_reboot(tmp_path):
    monitor = IOHealthMonitor(configparser.ConfigParser(), logging.getLogger(), str(tmp_path))
    for _ in range(5):
        assert not monitor.report_write_failure(OSError(errno.EROFS, 'readonly'), '/var/lib/meshcore-bot/test')
    assert not (tmp_path / 'meshbot_reboot.lock').exists()


def test_baliz_migrations_keep_usage_and_new_upstream_columns():
    from modules.db_migrations import MIGRATIONS
    versions = [entry[0] for entry in MIGRATIONS]
    assert len(versions) == len(set(versions))
    assert 'assistant' in dict((v, name) for v, name, fn in MIGRATIONS)[30]
    assert 'single-byte' in dict((v, name) for v, name, fn in MIGRATIONS)[31]
    assert 'scope_verdict' in dict((v, name) for v, name, fn in MIGRATIONS)[32]


def test_radio_markdown_cleanup_keeps_region_literals_and_command_placeholders():
    from modules.assistant.llm_service import LlmService
    assert LlmService._strip_markdown('**Région** : `#fr-bre`') == 'Région : #fr-bre'
    assert LlmService._strip_markdown('region add <region>') == 'region add <region>'
    assert LlmService._strip_markdown('#fr-bre') == '#fr-bre'


def test_upgrade_from_baliz_version_30_preserves_usage_data():
    import sqlite3
    from modules.db_migrations import MIGRATIONS, MigrationRunner
    conn = sqlite3.connect(':memory:')
    conn.executescript('''
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY, description TEXT, applied_at TEXT);
        CREATE TABLE assistant_function_usage (timestamp REAL, function TEXT);
        INSERT INTO assistant_function_usage VALUES (123, 'weather.forecast');
        CREATE TABLE daily_rollup (date TEXT);
        CREATE TABLE message_stats (timestamp REAL);
    ''')
    conn.executemany('INSERT INTO schema_version(version,description) VALUES (?, ?)', [(i, 'existing Baliz migration') for i in range(1,31)])
    conn.commit()
    MigrationRunner(conn, logging.getLogger()).run()
    assert conn.execute('SELECT * FROM assistant_function_usage').fetchall() == [(123, 'weather.forecast')]
    assert 'adverts_singlebyte_total' in {r[1] for r in conn.execute('PRAGMA table_info(daily_rollup)')}
    assert 'scope_verdict' in {r[1] for r in conn.execute('PRAGMA table_info(message_stats)')}
    assert conn.execute('SELECT MAX(version) FROM schema_version').fetchone()[0] == max(v for v, _, _ in MIGRATIONS)
    assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='bbs_inbox'").fetchone() == ('bbs_inbox',)
