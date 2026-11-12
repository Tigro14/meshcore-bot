import json
import sqlite3

import pytest

from modules.airtime_analysis import (
    AirtimeReader,
    build_report,
    create_tables,
    duty_summary,
    ingest_stream,
    normalize_packet,
)
from modules.channel_occupancy import lora_airtime_ms

MOD = {'sf': 9, 'bw_khz': 125.0, 'cr_den': 5, 'preamble': 8}
A, B, C, AUTHOR = 'aabb'+'11'*30, 'bbcc'+'22'*30, 'ccdd'+'33'*30, 'dead'+'44'*30
NOW = 2000000.0


@pytest.fixture
def conn():
    db = sqlite3.connect(':memory:')
    create_tables(db.cursor())
    db.execute('CREATE TABLE packet_stream(id INTEGER PRIMARY KEY,timestamp REAL,data TEXT,type TEXT)')
    db.execute('CREATE TABLE complete_contact_tracking(public_key TEXT,name TEXT,role TEXT)')
    db.executemany('INSERT INTO complete_contact_tracking VALUES (?,?,?)',
                   [(A, 'Relay A', 'repeater'), (B, 'Relay B', 'repeater'),
                    (C, 'Relay C', 'repeater'), (AUTHOR, 'User', 'companion')])
    yield db
    db.close()


def packet(path=('aabb', 'bbcc'), *, route='FLOOD', kind='GRP_TXT', payload=None, bph=2):
    if payload is None:
        payload = '0123456789abcdef'*5
    path_hex = ''.join(path)
    transport = '00000000' if route.startswith('TRANSPORT_') else ''
    raw = '00'+transport+'00'+path_hex+payload
    return {'path_hex': path_hex, 'route_type_name': route, 'payload_type_name': kind,
            'payload_hex': payload, 'bytes_per_hop': bph, 'raw_packet_hex': raw,
            'packet_hash': '0123456789abcdef'}


def insert(conn, pk, data, at=NOW-100, kind='packet'):
    conn.execute('INSERT INTO packet_stream VALUES (?,?,?,?)', (pk, at, json.dumps(data), kind))


def report(conn, window='1h'):
    while ingest_stream(conn, MOD)['processed']:
        pass
    return build_report(conn, now=NOW, window=window)


def test_full_frame_length_and_metadata_only(conn):
    p = packet(route='TRANSPORT_FLOOD')
    values = normalize_packet(p, NOW, 1, MOD)
    assert values[6] == 2+4+4+40
    assert values[10] == lora_airtime_ms(values[6], **MOD)
    insert(conn, 1, p)
    result = report(conn)
    assert result['local']['rx_packets'] == 1
    stored = repr(conn.execute('SELECT * FROM airtime_observations').fetchall())
    assert p['payload_hex'] not in stored
    assert p['raw_packet_hex'] not in stored
    assert result['schema_version'] == 1
    assert result['method']['compliance'] == 'not_determined'


def test_inconsistent_packet_is_not_silently_estimated():
    p = packet()
    p['raw_packet_hex'] += 'aa'
    assert normalize_packet(p, NOW, 1, MOD) is None
    p = packet();p['bytes_per_hop'] = 4
    assert normalize_packet(p, NOW, 1, MOD) is None


def test_shared_prefix_and_direct_observation_are_not_double_counted(conn):
    insert(conn, 1, packet(('aabb',)), NOW-120)
    insert(conn, 2, packet(('aabb', 'bbcc')), NOW-110)
    insert(conn, 3, packet(('aabb', 'ccdd')), NOW-100)
    r = report(conn)
    relays = {x['public_key']: x for x in r['repeaters']}
    assert relays[A]['packets'] == 1
    assert relays[A]['inferred_airtime_ms'] == 0
    assert relays[A]['direct_airtime_ms'] > 0
    assert relays[B]['packets'] == relays[C]['packets'] == 1
    assert r['local']['rx_packets'] == 3


def test_repeated_direct_receptions_are_real_emissions(conn):
    insert(conn, 1, packet(), NOW-110)
    insert(conn, 2, packet(), NOW-100)
    r = report(conn)
    relays = {x['public_key']: x for x in r['repeaters']}
    assert relays[B]['packets'] == 2
    assert relays[A]['packets'] == 1
    assert relays[A]['inferred_airtime_ms'] > 0
    assert r['quality']['collapsed_inferred_copies'] >= 1


def test_later_transmission_is_not_collapsed_forever(conn):
    insert(conn, 1, packet(), NOW-300)
    insert(conn, 2, packet(), NOW-100)
    assert {x['public_key']: x['packets'] for x in report(conn)['repeaters']} == {A: 2, B: 2}


@pytest.mark.parametrize('route,kind', [('DIRECT','TXT_MSG'), ('TRANSPORT_DIRECT','ACK'), ('DIRECT','TRACE'), ('FLOOD','TRACE')])
def test_route_instructions_and_trace_are_not_emission_history(conn, route, kind):
    insert(conn, 1, packet(route=route, kind=kind))
    r = report(conn)
    assert r['local']['rx_packets'] == 1
    assert r['repeaters'] == []
    assert r['quality']['excluded_route_packets'] == 1


def test_short_unambiguous_prefix_is_included_and_collision_is_not_guessed(conn):
    insert(conn, 1, packet(('aa',), bph=1))
    assert report(conn)['repeaters'][0]['public_key'] == A
    conn.execute('INSERT INTO complete_contact_tracking VALUES (?,?,?)', ('aaee'+'55'*30,'Collision','repeater'))
    r = build_report(conn, now=NOW, window='1h')
    assert r['repeaters'] == []
    assert r['quality']['ambiguous_hops'] == 1


def test_signed_advert_origin_counts_for_author_and_origin_relay(conn):
    insert(conn, 1, packet(('bbcc', 'ccdd'), payload=A+'00'*68, kind='ADVERT'))
    r = report(conn)
    assert r['authors'][0]['public_key'] == A
    assert r['authors'][0]['categories']['adverts']['packets'] == 1
    assert r['local']['unattributed_author_airtime_ms'] == 0
    relays = {x['public_key']: x for x in r['repeaters']}
    assert relays[A]['packets'] == 1


def test_message_metadata_correlates_author_without_copying_message_body(conn):
    insert(conn, 1, packet())
    insert(conn, 2, {'packet_hash':'0123456789abcdef','sender':'User','content':'PRIVATE MESSAGE BODY'}, kind='message')
    r = report(conn)
    assert r['authors'][0]['public_key'] == AUTHOR
    assert r['authors'][0]['categories']['messages']['packets'] == 1
    persisted = repr(conn.execute('SELECT * FROM airtime_author_links').fetchall())
    assert 'PRIVATE MESSAGE BODY' not in persisted
    assert 'PRIVATE MESSAGE BODY' not in json.dumps(r)


def test_unmatched_or_conflicting_authors_remain_unknown(conn):
    insert(conn, 1, packet())
    insert(conn, 2, {'packet_hash':'0123456789abcdef','sender':'User'}, kind='message')
    insert(conn, 3, {'packet_hash':'0123456789abcdef','sender':'Relay A'}, kind='message')
    r = report(conn)
    assert r['authors'] == []
    assert r['local']['unattributed_author_airtime_ms'] == r['local']['rx_airtime_ms']


def test_ingestion_is_idempotent_and_survives_restart(conn):
    insert(conn, 1, packet())
    assert ingest_stream(conn, MOD) == {'processed':1,'imported':1}
    assert ingest_stream(conn, MOD) == {'processed':0,'imported':0}
    assert conn.execute('SELECT count(*) FROM airtime_observations').fetchone()[0] == 1
    insert(conn, 2, packet(), NOW-20)
    assert report(conn)['local']['rx_packets'] == 2
    assert AirtimeReader().read(conn,'1h')['schema_version'] == 1


def test_no_observation_is_not_zero_or_compliance(conn):
    r = report(conn)
    assert r['local']['rx_percent'] is None
    assert r['coverage']['missing_intervals_are_unknown']
    assert duty_summary([], NOW-3600, NOW)['estimated_percent'] is None


def test_rolling_hour_crosses_clock_hour_and_no_green_compliance():
    events = [(NOW-61, 180000.0, True), (NOW-59, 190000.0, False)]
    d = duty_summary(events, NOW-86400, NOW)
    assert d['last_hour_percent'] == pytest.approx(370/3600*100, abs=1e-6)
    assert d['peak_hour_percent'] == d['last_hour_percent']
    assert d['status'] == 'reference_reached_in_estimate'
    assert d['compliance'] == 'unknown'


def test_window_changes_totals_without_erasing_older_metadata(conn):
    insert(conn, 1, packet(), NOW-7200)
    insert(conn, 2, packet(), NOW-30)
    assert report(conn,'1h')['local']['rx_packets'] == 1
    assert report(conn,'24h')['local']['rx_packets'] == 2


def test_partial_history_does_not_dilute_rx_over_unrecorded_days(conn):
    insert(conn, 1, packet(), NOW-7200)
    insert(conn, 2, packet(), NOW-30)
    r = report(conn, '7d')
    assert r['coverage']['available_seconds'] == 7200
    assert r['local']['rx_percent'] == pytest.approx(r['local']['rx_airtime_ms']/7200000*100, abs=1e-6)
    assert r['local']['requested_window_rx_percent'] < r['local']['rx_percent']
