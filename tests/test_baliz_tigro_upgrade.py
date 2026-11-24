"""Compatibility checks for the Tigro update on an existing Baliz installation."""

import logging
import sqlite3
from datetime import datetime, timedelta

import pytest

from modules.db_migrations import MIGRATIONS, MigrationRunner
from tests.conftest import mock_message
from tests.test_web_viewer_app import viewer_with_db  # noqa: F401, F811


@pytest.mark.parametrize('starting_version', [33, 34, 35])
def test_upgrade_from_deployed_baliz_keeps_usage_and_channel_consent(starting_version):
    conn = sqlite3.connect(':memory:')
    for version, _name, migrate in MIGRATIONS:
        if version <= starting_version:
            migrate(conn.cursor())
    conn.execute('CREATE TABLE schema_version(version INTEGER PRIMARY KEY, description TEXT, applied_at TEXT)')
    conn.executemany('INSERT INTO schema_version(version,description) VALUES (?,?)',
                     [(version, name) for version, name, _ in MIGRATIONS if version <= starting_version])
    conn.execute("INSERT INTO assistant_function_usage VALUES (123, 'weather.forecast')")
    conn.execute("INSERT INTO channel_web_visibility VALUES ('existing-channel-identity', 1)")
    conn.execute("INSERT INTO bbs_messages(sender_id,recipient_name,message) VALUES ('old-sender','recipient','legacy private message')")
    conn.execute("INSERT INTO clock_sync_targets(target,enabled,auto_clkreboot_enabled) VALUES ('existing-target',1,1)")
    if starting_version >= 35:
        conn.execute("INSERT INTO bbs_inbox(sender_key,recipient_key,message) VALUES ('saved-sender','saved-recipient','stored private message')")
    conn.commit()
    MigrationRunner(conn, logging.getLogger(__name__)).run()
    MigrationRunner(conn, logging.getLogger(__name__)).run()
    assert conn.execute('SELECT * FROM assistant_function_usage').fetchall() == [(123, 'weather.forecast')]
    assert conn.execute('SELECT * FROM channel_web_visibility').fetchall() == [('existing-channel-identity', 1)]
    assert conn.execute('SELECT MAX(version) FROM schema_version').fetchone()[0] == 36
    assert conn.execute('SELECT description FROM schema_version WHERE version=34').fetchone()[0] == 'channel occupancy buckets and sender share'
    assert conn.execute('SELECT message FROM bbs_messages').fetchall() == [('legacy private message',)]
    assert conn.execute('SELECT target,enabled,auto_clkreboot_enabled FROM clock_sync_targets').fetchall() == [('existing-target',1,1)]
    expected_inbox = [('stored private message', None, None)] if starting_version >= 35 else []
    assert conn.execute('SELECT message,reminder_sent_at,final_notice_sent_at FROM bbs_inbox').fetchall() == expected_inbox
    conn.execute("INSERT INTO bbs_inbox(sender_key,recipient_key,message) VALUES ('sender-key','recipient-key','new private message')")
    conn.commit()
    MigrationRunner(conn, logging.getLogger(__name__)).run()
    assert conn.execute('SELECT message FROM bbs_inbox ORDER BY id').fetchall() == [(row[0],) for row in expected_inbox] + [('new private message',)]
    assert conn.execute('SELECT COUNT(*) FROM occupancy_samples').fetchone()[0] == 0
    assert conn.execute('SELECT COUNT(*) FROM occupancy_senders').fetchone()[0] == 0


@pytest.mark.parametrize('module,klass,section', [
    ('test', 'TestCommand', 'Test_Command'), ('path', 'PathCommand', 'Path_Command'),
    ('hfcond', 'HfcondCommand', 'Hfcond_Command'), ('trace', 'TraceCommand', 'Trace_Command'),
    ('neighbors', 'NeighborsCommand', 'Neighbors_Command'), ('near', 'NearCommand', 'Near_Command'),
])
def test_internal_capability_keeps_access_checks_when_direct_command_hidden(command_mock_bot, module, klass, section):
    import importlib
    if not command_mock_bot.config.has_section(section):
        command_mock_bot.config.add_section(section)
    command_mock_bot.config.set(section, 'enabled', 'false')
    command = getattr(importlib.import_module(f'modules.commands.{module}_command'), klass)(command_mock_bot)
    command.cooldown_seconds = 0
    message = mock_message('question', is_dm=True)
    assert not command.can_execute(message)
    assert command.can_use_service(message)
    command.requires_dm = False
    command.allowed_channels = []
    message.is_dm = False
    message.channel = '#not-allowed'
    assert not command.can_use_service(message)
    assert command.can_use_service(message, skip_channel_check=True)
    command.requires_dm = True
    assert not command.can_use_service(message, skip_channel_check=True)


def test_period_advert_sort_and_totals_include_the_entire_filtered_cohort(viewer_with_db):  # noqa: F811
    viewer = viewer_with_db
    now = datetime.now()
    recent = (now - timedelta(hours=1)).isoformat(' ')
    with sqlite3.connect(viewer.db_path) as conn:
        for key, cumulative, packets in [('a'*64, 100, 1), ('b'*64, 10, 3)]:
            conn.execute('''INSERT INTO complete_contact_tracking
                         (public_key,name,role,device_type,last_heard,first_heard,advert_count)
                         VALUES (?,?,'repeater','Repeater',?,?,?)''',
                         (key, key[:1], recent, recent, cumulative))
            for index in range(packets):
                conn.execute('''INSERT INTO unique_advert_packets
                                (public_key,packet_hash,first_seen,date) VALUES (?,?,?,?)''',
                             (key, f'{key}-{index}', recent, now.date().isoformat()))
    kwargs = dict(page=1, page_size=1, sort='advert_count', direction='desc')
    daily = viewer._get_tracking_data(since='24h', **kwargs)
    assert 'error' not in daily, daily
    assert daily['tracking_data'][0]['user_id'] == 'b'*64
    assert daily['tracking_data'][0]['advert_count'] == 3
    assert daily['tracking_data'][0]['advert_counts']['all'] == 10
    assert daily['server_stats']['advertisements_24h'] == 4
    assert daily['server_stats']['total_advertisements'] == 110
    historical = viewer._get_tracking_data(since='all', **kwargs)
    assert historical['tracking_data'][0]['user_id'] == 'a'*64
    assert historical['tracking_data'][0]['advert_count'] == 100
    filtered = viewer._get_tracking_data(since='24h', search='b', **kwargs)
    assert filtered['server_stats']['advertisements_24h'] == 3
    assert filtered['server_stats']['total_advertisements'] == 10


def test_assistant_weather_keeps_provider_place_without_changing_direct_command_defaults():
    from unittest.mock import Mock

    from tests.unit.test_gwx_location_label import _gwx

    command = _gwx(default_country='FR')
    command.geocode_location = Mock(return_value=(47.88, -4.16, {'city': 'Combrit', 'country_code': 'fr'}, None))
    direct = command._get_weather_for_location_sync('Combrit', message=None)
    captured = command._get_weather_for_location_sync('Combrit', message=mock_message(capture_sink=[]))
    assert direct == 'Clear 20°C'
    assert captured.startswith('Combrit, FR: ')
    assert command.always_show_location is False
