import json
import random
import sqlite3

import pytest

from modules.airtime_analysis import (
    AirtimeReader,
    _reconstruct_emissions,
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


def test_later_direct_emission_does_not_erase_earlier_downstream_evidence(conn):
    insert(conn, 1, packet(('aabb', 'bbcc')), NOW-120)
    insert(conn, 2, packet(('aabb',)), NOW-110)
    relays = {x['public_key']: x for x in report(conn)['repeaters']}
    # A transmission received after the descendant cannot have caused it.
    assert relays[A]['packets'] == 2
    assert relays[A]['direct_airtime_ms'] > 0
    assert relays[A]['inferred_airtime_ms'] > 0


def test_downstream_copy_requires_enough_time_to_transmit_complete_next_frame(conn):
    insert(conn, 1, packet(('aabb',)), NOW-120)
    insert(conn, 2, packet(('aabb', 'bbcc')), NOW-120+0.0001)
    relays = {x['public_key']: x for x in report(conn)['repeaters']}
    assert relays[A]['packets'] == 2


def test_one_downstream_copy_does_not_add_to_two_direct_anchors(conn):
    insert(conn, 1, packet(('aabb',)), NOW-120)
    insert(conn, 2, packet(('aabb',)), NOW-110)
    insert(conn, 3, packet(('aabb', 'bbcc')), NOW-100)
    r = report(conn)
    relays = {x['public_key']: x for x in r['repeaters']}
    assert relays[A]['packets'] == 2
    assert relays[A]['inferred_airtime_ms'] == 0
    assert r['quality']['collapsed_inferred_copies'] == 1


def test_wire_format_and_modulation_are_part_of_emission_identity(conn):
    insert(conn, 1, packet(), NOW-120)
    ingest_stream(conn, MOD)
    insert(conn, 2, packet(), NOW-110)
    ingest_stream(conn, {**MOD, 'sf':10})
    insert(conn, 3, packet(route='TRANSPORT_FLOOD'), NOW-100)
    r = report(conn)
    assert next(x for x in r['repeaters'] if x['public_key'] == A)['packets'] == 3


def test_longer_path_uses_airtime_corrected_delay_not_a_fixed_reception_bucket():
    emissions, collapsed = _reconstruct_emissions([
        (100.0, 1000.0, True, 0.0),
        (169.0, 1000.0, False, 10.0),
    ])
    assert emissions == [(100.0, 1000.0, True)]
    assert collapsed == 1


def test_inferred_branches_need_a_common_possible_transmission_time():
    emissions, _ = _reconstruct_emissions([
        (100.0, 1000.0, False, 1.0),   # possible RX-end: [39,99]
        (101.0, 1000.0, False, 71.0),  # possible RX-end: [-30,30]
    ])
    assert len(emissions) == 2


def test_group_intersection_does_not_extend_forever_with_each_new_branch():
    emissions, _ = _reconstruct_emissions([
        (100.0, 1000.0, False, 1.0),
        (150.0, 1000.0, False, 1.0),
        (200.0, 1000.0, False, 1.0),
    ])
    assert len(emissions) == 2


def test_collapsed_copy_quality_only_counts_selected_period():
    _, collapsed = _reconstruct_emissions([
        (100.0, 1000.0, True, 0.0),
        (110.0, 1000.0, False, 1.0),
        (120.0, 1000.0, False, 1.0),
    ], count_since=115.0)
    assert collapsed == 1


def test_exact_hour_includes_events_straddling_minute_buckets():
    # 3599.5 seconds apart, but sixty minute-bucket indexes apart.
    events = [(60.1, 100000.0, True), (3659.6, 200000.0, True)]
    d = duty_summary(events, 0.0, 4000.0)
    assert d['peak_hour_percent'] == pytest.approx(300/3600*100, abs=1e-6)
    assert d['peak_hour_ending_at'] == 3659.6


def test_exact_hour_excludes_event_exactly_one_hour_old():
    d = duty_summary([(60.25, 100000.0, True), (3660.25, 200000.0, True)], 0.0, 4000.0)
    assert d['peak_hour_percent'] == pytest.approx(200/3600*100, abs=1e-6)


def test_peak_at_period_start_includes_lookbehind_before_next_event():
    events = [(500.25, 200000.0, True), (5500.5, 100000.0, True)]
    d = duty_summary(events, 4000.0, 6000.0)
    assert d['peak_hour_percent'] == pytest.approx(200/3600*100, abs=1e-6)
    assert d['peak_hour_ending_at'] == 4000.0
    assert d['last_hour_percent'] == pytest.approx(100/3600*100, abs=1e-6)


def test_event_sweep_matches_brute_force_with_fractional_times_and_duplicates():
    rng = random.Random(3659)
    for _ in range(25):
        start, end = 3600.25, 10800.75
        events = [(rng.randrange(11000)+rng.choice([0.0,0.25,0.75]),
                   rng.uniform(1, 10000), True) for _ in range(60)]
        events.extend([(start, 100.0, True), (end, 200.0, True), (end, 300.0, False)])
        rng.shuffle(events)
        check_at = [start] + [at for at, _, _ in events if start <= at <= end]
        expected = max(sum(ms for at, ms, _ in events if when-3600 < at <= when) for when in check_at)
        d = duty_summary(events, start, end)
        assert d['peak_hour_percent'] == pytest.approx(expected/3600000*100, abs=1e-6)


def test_report_describes_refined_method_without_changing_schema(conn):
    r = report(conn)
    assert r['schema_version'] == 1
    assert r['method']['version'] == 3
    assert r['method']['branch_dedup'] == 'causal_path_intervals'
    assert r['method']['peak_window'] == 'event_timestamps'
    assert r['method']['peak_resolution_seconds'] is None


def test_original_packet_count_and_radio_cost_deduplicate_shared_branches(conn):
    insert(conn, 1, packet(('aabb',)), NOW-120)
    insert(conn, 2, packet(('aabb','bbcc')), NOW-110)
    insert(conn, 3, packet(('aabb','ccdd')), NOW-100)
    insert(conn, 4, {'packet_hash':'0123456789abcdef','sender':'User'}, NOW-110, kind='message')
    r = report(conn)
    traffic, author = r['traffic'], r['authors'][0]
    assert traffic['distinct_packets'] == author['distinct_packets'] == 1
    assert traffic['received_packets'] == author['received_packets'] == 3
    assert traffic['additional_received_copies'] == 2
    assert author['categories']['messages']['distinct_packets'] == 1
    expected = sum(lora_airtime_ms(n, **MOD) for n in [42,44,46,46])
    assert traffic['reconstructed_radio_airtime_ms'] == pytest.approx(expected)
    assert author['reconstructed_radio_airtime_ms'] == pytest.approx(expected)
    assert traffic['cost_reconstructed_packets'] == 1


def test_participation_counts_distinct_original_packets_even_with_received_copies(conn):
    # Two sources converge on C. Repeated receipts do not increase participation.
    insert(conn, 1, packet(('aabb','ccdd')), NOW-120)
    insert(conn, 2, packet(('aabb','ccdd')), NOW-110)
    insert(conn, 3, packet(('bbcc','ccdd'), payload='ab'*40), NOW-100)
    r = report(conn)
    nodes = {node['public_key']:node for node in r['repeaters']}
    assert r['traffic']['path_packets'] == 2
    assert nodes[C]['path_packets'] == 2 and nodes[C]['participation_percent'] == 100
    assert nodes[A]['path_packets'] == nodes[B]['path_packets'] == 1
    assert nodes[A]['participation_percent'] == nodes[B]['participation_percent'] == 50
    assert nodes[C]['packets'] == 3  # Distinct directly received emission attempts.


def test_unknown_hop_cost_is_retained_without_guessing_its_identity(conn):
    insert(conn, 1, packet(('eeee','bbcc')))
    r = report(conn)
    assert {node['public_key'] for node in r['repeaters']} == {B}
    assert r['quality']['unknown_hops'] == 1
    assert r['traffic']['unresolved_relay_airtime_ms'] == lora_airtime_ms(44, **MOD)
    assert r['traffic']['reconstructed_radio_airtime_ms'] == pytest.approx(
        sum(lora_airtime_ms(n, **MOD) for n in [42,44,46]))
    assert r['traffic']['unknown_author']['distinct_packets'] == 1


def test_advert_origin_repeater_is_not_charged_twice_in_radio_cost(conn):
    insert(conn, 1, packet(('bbcc','ccdd'), payload=A+'00'*68, kind='ADVERT'))
    r = report(conn)
    assert r['traffic']['reconstructed_radio_airtime_ms'] == pytest.approx(
        sum(node['airtime_ms'] for node in r['repeaters']))
    assert r['authors'][0]['distinct_packets'] == 1
    assert r['authors'][0]['categories']['adverts']['distinct_packets'] == 1


def test_route_instructions_are_packets_but_have_no_invented_radio_cost(conn):
    insert(conn, 1, packet(route='DIRECT'))
    insert(conn, 2, {'packet_hash':'0123456789abcdef','sender':'User'}, kind='message')
    r = report(conn)
    assert r['traffic']['distinct_packets'] == 1
    assert r['traffic']['reconstructed_radio_airtime_ms'] is None
    assert r['traffic']['cost_unreconstructed_packets'] == 1
    assert r['authors'][0]['reconstructed_radio_airtime_ms'] is None
    assert r['traffic']['path_packets'] == 0


def test_authorship_from_one_copy_applies_to_other_copies_of_same_fingerprint(conn):
    insert(conn, 1, packet(), NOW-200)
    insert(conn, 2, packet(('ccdd',)), NOW-20)
    insert(conn, 3, {'packet_hash':'0123456789abcdef','sender':'User'}, NOW-20, kind='message')
    r = report(conn)
    assert len(r['authors']) == 1
    assert r['authors'][0]['distinct_packets'] == 1
    assert r['authors'][0]['received_packets'] == 2
    assert r['traffic']['unattributed_author_packets'] == 0
    assert r['local']['unattributed_author_airtime_ms'] == 0


def test_conflicting_copy_authorship_remains_one_unknown_original_packet(conn):
    insert(conn, 1, packet(), NOW-200)
    insert(conn, 2, {'packet_hash':'0123456789abcdef','sender':'User'}, NOW-200, kind='message')
    insert(conn, 3, packet(('ccdd',)), NOW-20)
    insert(conn, 4, {'packet_hash':'0123456789abcdef','sender':'Relay A'}, NOW-20, kind='message')
    r = report(conn)
    assert r['authors'] == []
    assert r['traffic']['unattributed_author_packets'] == 1
    assert r['traffic']['unknown_author']['received_packets'] == 2


def test_global_rx_has_no_per_transmitter_budget_comparison(conn):
    insert(conn, 1, packet())
    r = report(conn)
    assert 'reference_budget_used_percent' not in r['local']
    assert 'reference_percent' not in r['method']
    assert r['method']['duty_reference_scope'] == 'per_transmitter'
    assert all(node['reference_percent'] == 10.0 for node in r['repeaters'])


def test_trend_uses_equal_previous_period_without_counting_boundary_twice(conn):
    insert(conn, 1, packet(route='DIRECT', payload='ef'*40), NOW-10800)  # Retained history.
    insert(conn, 2, packet(('aabb',)), NOW-5400)
    insert(conn, 3, packet(('aabb',)), NOW-3600)  # In current period only.
    insert(conn, 4, packet(('aabb',)), NOW-100)
    r = report(conn)
    node = next(node for node in r['repeaters'] if node['public_key'] == A)
    assert r['comparison']['status'] == 'available_partial_observations'
    assert node['trend']['change_percent'] == 100
    assert node['trend']['previous_airtime_ms'] == pytest.approx(node['airtime_ms']/2)


def test_trend_missing_previous_node_is_unknown_not_a_zero_baseline(conn):
    insert(conn, 1, packet(route='DIRECT'), NOW-10800)
    insert(conn, 2, packet(('bbcc',)), NOW-100)
    node = report(conn)['repeaters'][0]
    assert node['trend']['status'] == 'no_previous_observation'
    assert node['trend']['change_percent'] is None


def test_trend_with_short_history_is_not_extrapolated(conn):
    insert(conn, 1, packet(), NOW-100)
    r = report(conn)
    assert r['comparison']['status'] == 'insufficient_history'
    assert all(node['trend']['change_percent'] is None for node in r['repeaters'])
