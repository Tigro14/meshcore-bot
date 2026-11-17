"""Independent checks for received copies, weighted radio cost, roles and trends."""
import pytest

from modules.airtime_capture import create_tables as capture_tables
from tests.test_airtime_analysis import AUTHOR, NOW, A, C, insert, packet, report
from tests.test_airtime_analysis import conn as conn


def test_copy_airtime_is_weighted_by_each_full_frame_not_by_packet_count(conn):
    insert(conn, 1, packet(('aabb',), payload='11'*10), NOW-300)
    insert(conn, 2, packet(('aabb', 'bbcc'), payload='11'*10), NOW-200)
    # Same short packet hash, different payload: this is a second original.
    insert(conn, 3, packet(('aabb',), payload='22'*180), NOW-100)
    r = report(conn)
    rows = conn.execute('SELECT airtime_ms FROM airtime_observations ORDER BY source_id').fetchall()
    expected = rows[1][0]/sum(row[0] for row in rows)*100
    copies = r['receptions']
    assert copies['distinct_packets'] == 2
    assert copies['received_packets'] == 3
    assert copies['repetition_factor'] == 1.5
    assert copies['additional_received_airtime_percent'] == pytest.approx(expected, abs=1e-6)
    assert abs(expected-100/3) > 5
    assert copies['rx_airtime_ms'] == pytest.approx(r['local']['rx_airtime_ms'])
    assert copies['first_received_airtime_ms']+copies['additional_received_airtime_ms'] == pytest.approx(copies['rx_airtime_ms'])


def test_selected_window_has_its_own_first_received_copy_and_bins_share_its_baseline(conn):
    insert(conn, 1, packet(), NOW-3700)
    insert(conn, 2, packet(), NOW-180)
    insert(conn, 3, packet(), NOW-60)
    r = report(conn)
    assert r['receptions']['received_packets'] == 2
    assert r['receptions']['additional_received_airtime_percent'] == 50
    populated = [point for point in r['local']['rx_series'] if point.get('distinct_packets')]
    assert [point['additional_received_airtime_percent'] for point in populated] == [0, 100]
    assert all(point['percent'] is None for point in r['local']['rx_series'] if point['distinct_packets'] is None)


def test_roles_keep_author_first_relay_and_last_emitter_separate(conn):
    insert(conn, 1, packet(('aabb', 'bbcc', 'ccdd')))
    r = report(conn)
    roles = r['receptions']['roles']
    assert roles['additive_across_roles'] is False
    assert [row['public_key'] for row in roles['entry_relays']] == [A]
    assert [row['public_key'] for row in roles['emitters']] == [C]
    assert r['authors'] == []
    assert roles['emitters'][0]['rx_airtime_ms'] == r['local']['rx_airtime_ms']
    assert roles['entry_relays'][0]['rx_airtime_ms'] == r['local']['rx_airtime_ms']


@pytest.mark.parametrize('route,kind', [('DIRECT', 'TXT_MSG'), ('TRANSPORT_DIRECT', 'ACK'), ('FLOOD', 'TRACE')])
def test_direct_instructions_and_trace_never_name_a_relay_emitter(conn, route, kind):
    insert(conn, 1, packet(route=route, kind=kind))
    roles = report(conn)['receptions']['roles']
    assert roles['emitters'] == roles['entry_relays'] == []
    assert roles['unknown_emitter']['received_packets'] == 1


def test_all_path_widths_resolve_without_multiplying_the_same_repeater(conn):
    for index, width in enumerate((1, 2, 3), 1):
        insert(conn, index, packet((A[:width*2],), bph=width), NOW-index)
    roles = report(conn)['receptions']['roles']
    assert len(roles['emitters']) == len(roles['entry_relays']) == 1
    assert roles['emitters'][0]['public_key'] == A
    assert roles['emitters'][0]['received_packets'] == 3


def test_ambiguous_prefix_remains_a_label_without_guessing_a_contact(conn):
    conn.execute('INSERT INTO complete_contact_tracking VALUES (?,?,?)', ('aa99'+'55'*30, 'Other A', 'repeater'))
    insert(conn, 1, packet(('aa',), bph=1))
    insert(conn, 2, packet(('bb',), bph=1, route='DIRECT'))
    r = report(conn)
    roles = r['receptions']['roles']
    row = roles['emitters'][0]
    assert row['public_key'] is None and row['prefix'] == 'aa'
    assert row['identification'] == 'ambiguous'
    assert sum(node['rx_airtime_ms'] for node in roles['emitters'])+roles['unknown_emitter']['rx_airtime_ms'] == pytest.approx(r['local']['rx_airtime_ms'])


def test_sources_rank_by_reconstructed_airtime_with_reconstruction_coverage(conn):
    for index in (1, 2):
        insert(conn, index, packet((), kind='ADVERT', payload=A+'11'*68+f'{index:02x}'), NOW-200+index)
    insert(conn, 3, packet(('aabb', 'bbcc', 'ccdd'), kind='ADVERT', payload=AUTHOR+'22'*160))
    r = report(conn)
    assert [node['public_key'] for node in r['authors']] == [AUTHOR, A]
    assert [node['distinct_packets'] for node in r['authors']] == [1, 2]
    assert all(node['cost_reconstruction_coverage_percent'] == 100 for node in r['authors'])


def test_identified_author_heard_without_flood_relay_is_an_emitter_without_entry_relay(conn):
    insert(conn, 1, packet((), kind='ADVERT', payload=AUTHOR+'22'*68))
    roles = report(conn)['receptions']['roles']
    assert roles['entry_relays'] == []
    assert roles['emitters'][0]['public_key'] == AUTHOR
    assert roles['unknown_emitter']['received_packets'] == 0


def test_truncated_input_does_not_claim_a_full_period_copy_ratio(conn, monkeypatch):
    for index in (1, 2, 3):
        insert(conn, index, packet(), NOW-400+index)
    monkeypatch.setattr('modules.airtime_analysis.MAX_ROWS', 2)
    copies = report(conn)['receptions']
    assert copies['status'] == 'truncated'
    assert copies['repetition_factor'] is None
    assert copies['additional_received_airtime_percent'] is None


@pytest.mark.parametrize('continuous', [False, True])
def test_period_comparison_requires_continuous_listening_in_both_windows(conn, continuous):
    capture_tables(conn.cursor())
    if continuous:
        conn.execute('INSERT INTO airtime_capture_intervals VALUES (?,?)', (NOW-7200, NOW))
    for index, at in enumerate((NOW-7200, NOW-4000, NOW-100, NOW-50), 1):
        insert(conn, index, packet(), at)
    comparison = report(conn)['receptions']['comparison']
    assert comparison['status'] == ('comparable' if continuous else 'incomplete_observation')
    assert comparison['previous']['received_packets'] == 2
    assert comparison['additional_airtime_change_points'] == (0 if continuous else None)


def test_no_receptions_is_unknown_share_not_an_invented_zero(conn):
    copies = report(conn)['receptions']
    assert copies['status'] == 'no_receptions'
    assert copies['repetition_factor'] is None
    assert copies['additional_received_airtime_percent'] is None


def test_live_comparison_closes_at_the_last_confirmed_tick_without_filling_an_internal_gap(conn):
    capture_tables(conn.cursor())
    conn.execute('INSERT INTO airtime_capture_intervals VALUES (?,?)', (NOW-7205, NOW-5))
    for index, at in enumerate((NOW-7205, NOW-4000, NOW-100, NOW-50), 1):
        insert(conn, index, packet(), at)
    comparison = report(conn)['receptions']['comparison']
    assert comparison['status'] == 'comparable'
    assert comparison['end_time'] == NOW-5
    assert comparison['window_seconds'] == 3600
    assert comparison['previous']['received_packets'] == comparison['current']['received_packets'] == 2
    conn.execute('UPDATE airtime_capture_intervals SET end_at=?', (NOW-100,))
    conn.execute('INSERT INTO airtime_capture_intervals VALUES (?,?)', (NOW-50, NOW-5))
    assert report(conn)['receptions']['comparison']['additional_airtime_change_points'] is None
