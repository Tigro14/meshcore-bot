"""Map clock evidence is dated, signed and independent of transport ACKs."""

import json
import sqlite3
import time
from configparser import ConfigParser
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from modules.clock_observations import map_clock_fields
from tests.test_web_viewer_app import viewer_with_db as viewer_with_db


@pytest.mark.parametrize('drift,flag', [(0, False), (300, False), (301, True), (1793177892, True)])
def test_map_warning_uses_existing_threshold_and_preserves_invalid_clock_difference(drift, flag):
    config = ConfigParser()
    config.read_dict({'Bot': {}, 'Clock_Sync_Admin': {'dashboard_max_clock_drift_seconds': '300'}})
    sample = {'drift': drift, 'received_at': 1793890000, 'sender_timestamp': 1793890000 - drift,
              'source': 'advert', 'timestamp_valid': drift < 1793177892}
    fields = map_clock_fields(sample, config, now=1793890000)
    assert fields['clock_drift_exceeded'] is flag
    assert fields['clock_drift_seconds'] == drift
    assert fields['clock_signed_drift_seconds'] == -drift


def test_old_clock_warning_is_explicitly_dated_instead_of_silently_cleared():
    config = ConfigParser()
    config.read_dict({'Bot': {}, 'Clock_Sync_Admin': {'dashboard_max_clock_drift_seconds': '600'}})
    sample = {'drift': 1000, 'received_at': 1793890000, 'sender_timestamp': 1793889000,
              'source': 'advert', 'timestamp_valid': True}
    fields = map_clock_fields(sample, config, now=1793890000 + 7 * 86400)
    assert fields['clock_drift_exceeded'] is True
    assert fields['clock_observation_stale'] is True
    assert fields['clock_observed_at'] == sample['received_at']
    assert fields['clock_drift_threshold_seconds'] == 600
    assert map_clock_fields(None, config)['clock_drift_exceeded'] is False


def test_mesh_node_api_uses_latest_clock_evidence_and_keeps_node_filters(viewer_with_db):
    viewer = viewer_with_db
    viewer.config.set('Bot', 'timezone', 'Europe/Paris')
    now = int(time.time())
    nodes = [('Behind', 'ab'*32, -601, 0, 'repeater'),
             ('Ahead', 'cd'*32, 601, 0, 'repeater'),
             ('Within', 'ef'*32, 300, 0, 'repeater'),
             ('Unknown', '12'*32, None, 0, 'repeater'),
             ('OldBad', '34'*32, -1000, 8*86400, 'repeater'),
             ('FreshMessage', '56'*32, -1000, 3600, 'repeater'),
             ('Companion', '78'*32, -1000, 0, 'companion')]
    with sqlite3.connect(viewer.db_path) as db:
        db.execute('CREATE TABLE IF NOT EXISTS message_stats (id INTEGER PRIMARY KEY, sender_id TEXT, timestamp INTEGER, created_at TEXT)')
        for name, key, delta, age, role in nodes:
            local = datetime.fromtimestamp(now-age, ZoneInfo('Europe/Paris')).strftime('%Y-%m-%d %H:%M:%S')
            raw = {} if delta is None else {'advert_time': now-age+delta}
            db.execute('''INSERT INTO complete_contact_tracking
                (public_key,name,role,last_heard,latitude,longitude,raw_advert_data)
                VALUES (?,?,?,?,48.1,-3.2,?)''', (key, name, role, local, json.dumps(raw)))
        utc = datetime.fromtimestamp(now, timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        db.execute('INSERT INTO message_stats (sender_id,timestamp,created_at) VALUES (?,?,?)', ('FreshMessage', now-9, utc))
    with viewer.app.test_client() as client:
        response = client.get('/api/mesh/nodes')
        assert response.status_code == 200
        rows = {node['name']: node for node in response.get_json()['nodes']}
        assert 'Companion' not in rows
        assert rows['Behind']['clock_drift_exceeded'] is True
        assert rows['Behind']['clock_signed_drift_seconds'] == -601
        assert rows['Ahead']['clock_signed_drift_seconds'] == 601
        assert rows['Within']['clock_drift_exceeded'] is False
        assert rows['Unknown']['clock_drift_seconds'] is None
        assert rows['Unknown']['clock_drift_exceeded'] is False
        assert rows['OldBad']['clock_drift_exceeded'] is True
        assert rows['OldBad']['clock_observation_stale'] is True
        assert rows['FreshMessage']['clock_drift_seconds'] == 9
        assert rows['FreshMessage']['clock_drift_exceeded'] is False
        assert rows['FreshMessage']['clock_drift_source'] == 'message'
        recent = {node['name'] for node in client.get('/api/mesh/nodes?days=7').get_json()['nodes']}
        assert 'OldBad' not in recent
        assert 'Behind' in recent
