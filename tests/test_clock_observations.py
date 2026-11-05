"""Clock evidence uses the correct time basis and never treats ACK as sync."""

import json
import sqlite3
from configparser import ConfigParser
from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from modules.clock_observations import latest_clock_samples, parse_timestamp, target_clock_status, utc_iso
from modules.scheduler import MessageScheduler
from modules.web_viewer.app import BotDataViewer
from tests.test_web_viewer_app import viewer_with_db as viewer_with_db

NOW = datetime(2026, 11, 5, 14, 3, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def config():
    result = ConfigParser()
    result.read_dict({'Bot': {'timezone': 'Europe/Paris'}, 'Clock_Sync_Admin': {
        'min_sync_drift': '60', 'dashboard_check_window_hours': '24',
    }})
    return result


def evidence(drift=9, received=NOW - 10, valid=True):
    return {'drift': drift, 'received_at': received, 'sender_timestamp': int(received - drift),
            'source': 'message', 'timestamp_valid': valid}


def test_sqlite_utc_and_local_advert_have_same_epoch(config):
    utc = parse_timestamp('2026-11-05 14:00:45', utc=True)
    local = parse_timestamp('2026-11-05 15:00:45', config=config)
    assert utc == local
    assert utc_iso('2026-11-05 14:00:45') == '2026-11-05T14:00:45+00:00'


@pytest.mark.parametrize('local,utc', [('2026-07-05 15:00:00', '2026-07-05 13:00:00'),
                                    ('2026-11-05 15:00:00', '2026-11-05 14:00:00')])
def test_summer_and_winter_offsets_are_source_aware(config, local, utc):
    assert parse_timestamp(local, config=config) == parse_timestamp(utc, utc=True)


def test_scheduler_contacts_and_time_share_exact_same_samples(config, monkeypatch):
    with sqlite3.connect(':memory:') as db:
        db.row_factory = sqlite3.Row
        db.executescript('''
            CREATE TABLE complete_contact_tracking (name TEXT, last_heard TEXT, raw_advert_data TEXT);
            CREATE TABLE message_stats (id INTEGER PRIMARY KEY, sender_id TEXT, created_at TEXT, timestamp INTEGER);
        ''')
        db.executemany('INSERT INTO complete_contact_tracking VALUES (?,?,?)', [
            ('RelayA', '2026-11-04 19:49:09.249557', json.dumps({'advert_time': 1793818176})),
            ('RelayB', '2026-11-04 22:07:09.374754', json.dumps({'advert_time': 1793826421})),
            ('RelayC', '2026-11-05 01:58:15.959008', json.dumps({'advert_time': 662403})),
        ])
        db.execute('INSERT INTO message_stats VALUES (1,?,?,?)', ('RelayB', '2026-11-05 11:52:11', 1793879522))
        viewer = BotDataViewer.__new__(BotDataViewer)
        viewer.config = config; viewer.logger = Mock()
        scheduler = MessageScheduler.__new__(MessageScheduler)
        scheduler.bot = SimpleNamespace(config=config, db_manager=SimpleNamespace(connection=lambda: nullcontext(db)))
        scheduler.logger = Mock()
        monkeypatch.setattr('modules.scheduler.time.time', lambda: NOW)
        samples = viewer._get_latest_clock_drift_samples(db.cursor())
        assert samples == latest_clock_samples(db.cursor(), config)
        for name, expected in [('RelayA', 27), ('RelayB', 9), ('RelayC', 1793177892)]:
            assert samples[name]['drift'] == expected
            assert scheduler._get_clock_sync_target_drift(name) == expected
        assert samples['RelayC']['timestamp_valid'] is False


@pytest.mark.parametrize('sample', [None, evidence(27, NOW - 3600), evidence(1793177892, NOW - 3600, False)])
def test_ack_without_new_measurement_always_needs_verification(config, sample):
    result = target_clock_status(sample, {'success': True, 'at': NOW - 60}, config, now=NOW)
    assert result['clock_state'] == 'awaiting_verification'
    assert result['sync_delivery_state'] == 'confirmed'


@pytest.mark.parametrize('sample,state', [(evidence(), 'in_sync'), (evidence(200), 'warning'),
    (evidence(600), 'out_of_sync'), (evidence(1793177892, valid=False), 'unset')])
def test_measurement_after_ack_can_confirm_or_disprove_clock_sync(config, sample, state):
    assert target_clock_status(sample, {'success': True, 'at': NOW - 60}, config, now=NOW)['clock_state'] == state


def test_delivery_failure_does_not_override_a_good_clock_measurement(config):
    result = target_clock_status(evidence(), {'success': False, 'at': NOW - 1}, config, now=NOW)
    assert result['clock_state'] == 'in_sync'
    assert result['sync_delivery_state'] == 'unconfirmed'


def test_firmware_rejection_is_not_reported_as_ok(config):
    result = target_clock_status(evidence(received=NOW - 100),
        {'success': True, 'at': NOW - 60, 'error': 'ERR: clock cannot go backwards'}, config, now=NOW)
    assert result['clock_state'] == 'rejected'
    assert result['sync_delivery_state'] == 'confirmed'


def test_stale_measurement_does_not_claim_current_clock_is_good(config):
    assert target_clock_status(evidence(received=NOW - 90000), None, config, now=NOW)['clock_state'] == 'stale'


def test_time_api_and_contacts_show_the_same_evidence_and_keep_ack_separate(viewer_with_db):
    import time
    from zoneinfo import ZoneInfo

    viewer = viewer_with_db
    viewer.config.set('Bot', 'timezone', 'Europe/Paris')
    now = int(time.time())
    old = now - 3600
    local = datetime.fromtimestamp(old, ZoneInfo('Europe/Paris')).strftime('%Y-%m-%d %H:%M:%S')
    utc = datetime.fromtimestamp(old, timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    sent = datetime.fromtimestamp(now - 60, timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    with sqlite3.connect(viewer.db_path) as db:
        db.execute('CREATE TABLE IF NOT EXISTS message_stats (id INTEGER PRIMARY KEY, sender_id TEXT, timestamp INTEGER, created_at TEXT)')
        for name, key, sender in [('RelayA', 'ab'*32, old + 27), ('RelayB', 'cd'*32, old - 30),
                                  ('RelayC', 'ef'*32, 662403)]:
            db.execute('INSERT INTO complete_contact_tracking (name,public_key,role,last_heard,raw_advert_data) VALUES (?,?,?,?,?)',
                       (name, key, 'repeater', local, json.dumps({'advert_time': sender})))
            db.execute('INSERT INTO clock_sync_targets (target) VALUES (?)', (key,))
            db.execute('INSERT INTO clock_sync_admin_log (public_key,target_name,success,sent_at) VALUES (?,?,?,?)',
                       (key, name, 0 if name == 'RelayB' else 1, sent))
        db.execute('INSERT INTO message_stats (sender_id,created_at,timestamp) VALUES (?,?,?)', ('RelayB', utc, old - 9))
    with viewer.app.test_client() as client:
        response = client.get('/api/clock-sync-targets-admin')
        assert response.status_code == 200
        rows = {row['contact_name']: row for row in response.get_json()['targets']}
        assert rows['RelayA']['clock_drift'] == 27
        assert rows['RelayA']['clock_state'] == 'awaiting_verification'
        assert rows['RelayB']['clock_drift'] == 9
        assert rows['RelayB']['clock_state'] == 'in_sync'
        assert rows['RelayC']['clock_state'] == 'awaiting_verification'
        assert rows['RelayC']['clock_timestamp_valid'] is False
        assert rows['RelayC']['clock_drift'] == old - 662403
        assert rows['RelayC']['last_sync_at'].endswith('+00:00')
        history = client.get('/api/clock-sync-targets-admin/1/history').get_json()['history']
        assert history[0]['at'].endswith('+00:00')
    contacts = {row['username']: row for row in viewer._get_tracking_data(since='all')['tracking_data']}
    for name in rows:
        assert contacts[name]['clock_drift_seconds'] == rows[name]['clock_drift']
        assert contacts[name]['clock_drift_status'] == rows[name]['clock_drift_status']
        assert contacts[name]['clock_observed_at'] == rows[name]['clock_observed_at']
    assert contacts['RelayC']['clock_drift_status'] == 'unset'
    assert contacts['RelayC']['clock_state'] == 'awaiting_verification'
    assert contacts['RelayC']['clock_timestamp_valid'] is False
    assert contacts['RelayB']['clock_drift_status'] == 'in_sync'


def test_auto_clkreboot_setting_survives_reads_and_preserves_other_target_fields(viewer_with_db):
    viewer = viewer_with_db
    key = 'ab' * 32
    with sqlite3.connect(viewer.db_path) as db:
        target_id = db.execute('INSERT INTO clock_sync_targets (target,enabled) VALUES (?,1)', (key,)).lastrowid
    with viewer.app.test_client() as client:
        for enabled in (True, False):
            result = client.put(f'/api/clock-sync-targets-admin/{target_id}',
                json={'auto_clkreboot_enabled': enabled}, headers={'X-Requested-With': 'XMLHttpRequest'})
            assert result.status_code == 200
            assert result.get_json()['auto_clkreboot_enabled'] is enabled
            for _ in range(2):
                saved = client.get('/api/clock-sync-targets-admin').get_json()['targets'][0]
                assert saved['auto_clkreboot_enabled'] is enabled
            with sqlite3.connect(viewer.db_path) as db:
                row = db.execute('SELECT auto_clkreboot_enabled,enabled,last_clkreboot_at FROM clock_sync_targets WHERE id=?', (target_id,)).fetchone()
                assert row == (int(enabled), 1, None)
