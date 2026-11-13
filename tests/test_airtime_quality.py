"""Local offered load must not sum distant nodes or fabricate expected forwards."""
import json
import random
import sqlite3

import pytest

from modules.airtime_analysis import Contacts, build_report, create_tables, duty_summary, ingest_stream
from modules.airtime_quality import (
    local_load,
    observer_metadata,
    reception_history,
    rolling_peak,
    write_observer_metadata,
)
from modules.channel_occupancy import lora_airtime_ms

A, B, SELF = 'aaaa'+'11'*30, 'bbbb'+'22'*30, 'cccc'+'33'*30
MOD = {'sf': 8, 'bw_khz': 62.5, 'cr_den': 8, 'preamble': 8}


@pytest.fixture
def db():
    conn = sqlite3.connect(':memory:')
    create_tables(conn.cursor())
    conn.execute('CREATE TABLE complete_contact_tracking(public_key TEXT,name TEXT,role TEXT)')
    conn.executemany('INSERT INTO complete_contact_tracking VALUES (?,?,?)',
                    [(A, 'Remote relay', 'repeater'), (B, 'Local neighbour', 'repeater'), (SELF, 'Baliz', 'companion')])
    conn.execute('CREATE TABLE packet_stream(id INTEGER PRIMARY KEY,timestamp REAL,data TEXT,type TEXT)')
    conn.execute('CREATE TABLE bot_metadata(key TEXT PRIMARY KEY,value TEXT,updated_at REAL)')
    yield conn
    conn.close()


def test_local_load_uses_neighbours_not_every_relay_in_paths(db):
    events = {A: [(150, 1000, False)], B: [(160, 1500, True), (200, 1500, False)]}
    result = local_load(events, Contacts(db), {}, 0, 3600, 0, 2000, 0)
    assert result['airtime_ms'] == 3000
    assert result['neighbour_count'] == 1
    assert result['neighbours'][0]['public_key'] == B
    assert result['directly_attributed_rx_percent'] == 75
    assert result['estimated_percent'] == pytest.approx(3/3600*100, abs=1e-6)
    assert result['expected_ecl_percent'] is None
    assert result['neighbours'][0]['fdr']['expected'] is None
    assert result['neighbours'][0]['fdr']['percent'] is None
    assert result['is_duty_cycle'] is False


def test_ecl_does_not_add_rx_again_or_clip_overlapping_emissions(db):
    events = {A: [(10, 200000, True)], B: [(10, 200000, True)]}
    result = local_load(events, Contacts(db), {}, 0, 300, 0, 400000, 0)
    assert result['estimated_percent'] == pytest.approx(133.333333)
    assert result['airtime_ms'] == 400000  # Not 800000: RX is not added again.
    assert result['peak_five_minutes']['percent'] > 100


def test_evidenced_companion_origins_count_as_own_tx_not_neighbours(db):
    events = {SELF: [(200, 1000, False)], B: [(210, 1500, True)]}
    result = local_load(events, Contacts(db), {'public_key': SELF}, 0, 3600, 0, 1500, 0)
    assert result['observer_evidenced_airtime_ms'] == 1000
    assert result['airtime_ms'] == 2500
    assert result['neighbour_count'] == 1
    assert result['observer_tx_complete'] is False


def test_missing_neighbour_or_missing_buckets_are_unknown(db):
    result = local_load({A: [(200, 1000, False)]}, Contacts(db), {}, 0, 3600, 0, 1500, 0)
    assert result['estimated_percent'] is None
    assert result['neighbour_count'] == 0
    assert all(bucket['percent'] is None for bucket in result['buckets'])
    assert result['peak_five_minutes']['percent'] is None
    assert result['capture_uptime_known'] is False


def test_new_history_and_bucket_edges_do_not_look_like_saturation(db):
    result = local_load({B: [(3600, 1000, True)]}, Contacts(db), {}, 0, 3600, 3600, 1000, 3600)
    assert result['estimated_percent'] is None
    assert result['buckets'][-1]['percent'] == pytest.approx(1/300*100, abs=1e-6)
    assert result['buckets'][-1]['partial_history'] is True
    assert all(bucket['start_time'] < bucket['end_time'] for bucket in result['buckets'])
    assert result['retained_full_hour'] is False


def test_five_minute_peak_has_exact_left_boundary_and_lookbehind():
    events = [(100, 1000, True), (399.9, 2000, False), (400, 3000, True)]
    result = rolling_peak(events, 350, 450, 300)
    assert result['percent'] == pytest.approx(5000/300000*100, abs=1e-6)
    assert result['ending_at'] == 400
    assert rolling_peak([(100, 1000, True)], 350, 450, 300)['ending_at'] == 350


def test_rolling_burst_matches_independent_brute_force():
    rng = random.Random(13)
    for _ in range(30):
        events = [(rng.uniform(0, 7200), rng.uniform(100, 5000), True) for _ in range(50)]
        expected = max(sum(ms for at, ms, _ in events if point-300 < at <= point)
                       for point in [3600] + [at for at, _, _ in events if at >= 3600])
        assert rolling_peak(events, 3600, 7200, 300)['percent'] == pytest.approx(expected/300000*100, abs=1e-6)


def test_dc_evidence_is_independent_of_five_minute_burst():
    result = duty_summary([(3500, 3000, True), (3599, 2000, False)], 0, 3600)
    assert result['last_hour_direct_percent'] == pytest.approx(3000/3600000*100, abs=1e-6)
    assert result['last_hour_inferred_percent'] == pytest.approx(2000/3600000*100, abs=1e-6)
    assert result['direct_packets'] == result['inferred_packets'] == 1
    assert result['peak_five_minute_percent'] == pytest.approx(result['peak_hour_percent']*12, abs=1e-5)
    assert result['reference_percent'] == 10
    assert result['compliance'] == 'unknown'


def test_old_daily_evidence_does_not_claim_a_silent_last_hour():
    result = duty_summary([(100, 1000, True)], 0, 86400)
    assert result['packets'] == 1
    assert result['last_hour_percent'] is None
    assert result['last_hour_direct_percent'] is None
    assert result['last_hour_inferred_percent'] is None
    assert result['last_hour_packets'] == 0
    assert result['peak_hour_percent'] > 0


def test_observer_snapshot_contains_only_public_identity(db):
    write_observer_metadata(db, {'public_key': SELF, 'type': 1, 'channel_key': 'secret', 'name': 'private'})
    assert observer_metadata(db) == {'public_key': SELF, 'role': 'companion'}
    stored = db.execute('SELECT value FROM bot_metadata').fetchone()[0]
    assert 'secret' not in stored and 'private' not in stored
    write_observer_metadata(db, {'public_key': 'bad'})
    assert observer_metadata(db)['public_key'] == SELF


def test_cr8_full_frames_and_own_reply_correlation_are_preserved(db):
    write_observer_metadata(db, {'public_key': SELF, 'type': 1})
    payload = '11'*40
    packet = {'path_hex': 'aaaabbbb', 'route_type_name': 'FLOOD', 'payload_type_name': 'GRP_TXT',
              'bytes_per_hop': 2, 'payload_hex': payload, 'raw_packet_hex': '0000aaaabbbb'+payload,
              'packet_hash': '0123456789abcdef'}
    db.execute('INSERT INTO packet_stream VALUES (1,3500,?,\'packet\')', (json.dumps(packet),))
    db.execute('INSERT INTO packet_stream VALUES (2,3510,?,\'command\')',
               (json.dumps({'reply_packet_hash': packet['packet_hash'], 'response': 'PRIVATE TEXT'}),))
    ingest_stream(db, MOD)
    result = build_report(db, now=3600, window='1h')
    assert result['method']['modulation_profiles'] == [MOD]
    assert result['local']['rx_airtime_ms'] == lora_airtime_ms(46, **MOD)
    nonempty = [point['percent'] for point in result['local']['rx_series'] if point['percent'] is not None]
    assert nonempty == [pytest.approx(result['local']['rx_airtime_ms']/300000*100, abs=1e-6)]
    assert result['local_load']['neighbour_count'] == 1
    assert result['local_load']['observer_evidenced_airtime_ms'] == pytest.approx(lora_airtime_ms(42, **MOD), abs=.001)
    assert result['traffic']['identified_author_packets'] == 1
    assert len(result['repeaters']) == 2  # Baliz is not a repeater.
    assert next(node for node in result['repeaters'] if node['public_key'] == B)['local_neighbour'] is True
    assert next(node for node in result['repeaters'] if node['public_key'] == A)['local_neighbour'] is False
    assert 'PRIVATE TEXT' not in repr(db.execute('SELECT * FROM airtime_author_links').fetchall())


def test_rx_history_uses_full_frame_airtime_and_preserves_unknown_gaps():
    result = reception_history([(0, 1000, True), (299, 2000, True), (900, 6000, True)], 0, 900)
    assert result['rx_bucket_seconds'] == 300
    assert [point['end_time'] for point in result['rx_series']] == [300, 600, 900]
    assert [point['percent'] for point in result['rx_series']] == [1, None, 2]
    assert result['rx_peak_five_minute_percent'] == 2


def test_rx_history_rolling_peak_uses_lookbehind_but_series_does_not():
    result = reception_history([(50, 2000, True), (301, 1000, True)], 300, 900)
    assert result['rx_series'][0]['percent'] == pytest.approx(1/3, abs=1e-6)
    assert result['rx_peak_five_minute_percent'] == 1
    empty = reception_history([(50, 2000, True)], 300, 900)
    assert empty['rx_peak_five_minute_percent'] is None
    assert all(point['percent'] is None for point in empty['rx_series'])


def test_weekly_chart_downsamples_but_keeps_exact_five_minute_peak():
    result = reception_history([(3500, 3000, True), (3601, 3000, True)], 0, 7*86400)
    assert result['rx_bucket_seconds'] == 3600
    assert len(result['rx_series']) == 168
    assert result['rx_series'][0]['percent'] == pytest.approx(3/3600*100, abs=1e-6)
    assert result['rx_peak_five_minute_percent'] == 2
