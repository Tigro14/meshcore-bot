import configparser
import hashlib
import hmac
import json
import sqlite3
from types import SimpleNamespace

import pytest

from modules.airtime_analysis import Contacts, create_tables, ingest_stream
from modules.airtime_forwarding import analysis_scope_keys, build_forwarding_model, create_context_tables, packet_scope
from modules.channel_occupancy import lora_airtime_ms

A, B, C, D = ('aaaa'+'11'*30, 'bbbb'+'22'*30, 'cccc'+'33'*30, 'dddd'+'44'*30)
END = 2000000.0
START = END-3600
MOD = {'sf': 8, 'bw_khz': 62.5, 'cr_den': 8, 'preamble': 8}
KEYS = {'#region-one': b'a'*16, '#region-two': b'b'*16}


@pytest.fixture
def db():
    conn = sqlite3.connect(':memory:')
    create_tables(conn.cursor())
    create_context_tables(conn.cursor())
    conn.execute('CREATE TABLE packet_stream(id INTEGER PRIMARY KEY,timestamp REAL,data TEXT,type TEXT)')
    conn.execute('CREATE TABLE complete_contact_tracking(public_key TEXT,name TEXT,role TEXT)')
    conn.executemany('INSERT INTO complete_contact_tracking VALUES (?,?,?)',
                    [(A, 'A', 'repeater'), (B, 'B', 'repeater'), (C, 'Origin', 'companion'), (D, 'D', 'repeater')])
    yield conn
    conn.close()


def add(db, at, path, *, nonce=1, kind='GRP_TXT', scope='global', bph=2, route=None):
    payload = nonce.to_bytes(4, 'little')+b'private-encrypted-payload'
    route = route or ('FLOOD' if scope == 'global' else 'TRANSPORT_FLOOD')
    type_id = 5 if kind == 'GRP_TXT' else 4
    code = int.from_bytes(hmac.new(KEYS.get(scope, b'z'*16), bytes([type_id])+payload, hashlib.sha256).digest()[:2], 'little')
    code = 1 if code == 0 else 0xfffe if code == 0xffff else code
    transport = code.to_bytes(2, 'little')+b'\0\0' if route.startswith('TRANSPORT_') else b''
    path_hex = ''.join(key[:bph*2] for key in path)
    packet = {'route_type_name': route, 'payload_type_name': kind, 'payload_type': type_id,
              'payload_hex': payload.hex(), 'bytes_per_hop': bph, 'path_hex': path_hex,
              'transport_codes': {'code1': code, 'code2': 0},
              'raw_packet_hex': '00'+transport.hex()+'00'+path_hex+payload.hex()}
    source = db.execute('SELECT COALESCE(MAX(id),0)+1 FROM packet_stream').fetchone()[0]
    db.execute('INSERT INTO packet_stream VALUES (?,?,?,\'packet\')', (source, at, json.dumps(packet)))
    return packet


def train(db, **kwargs):
    for index, age in enumerate([8*3600, 2*3600, 1000]):
        add(db, START-age, [A, B], nonce=100+index, **kwargs)


def model(db, *, keys=KEYS, local_keys=None, events=None, author=C, observer=None, limit=100000):
    while ingest_stream(db, MOD, scope_keys=keys, limit=limit)['processed']:
        pass
    authors = {fp: author for fp, in db.execute('SELECT fingerprint FROM airtime_observations WHERE observed_at>=?', (START,))} if author else {}
    return build_forwarding_model(db, Contacts(db), start=START, end=END, authors=authors,
        evidence_events=events or {}, local_keys=local_keys or set(), local_airtime_ms=1000,
        available_seconds=3600, observer_key=observer)


def test_supported_missing_branch_has_cr8_full_frame_cost(db):
    train(db)
    packet = add(db, END-120, [A], nonce=1)
    result = model(db)
    assert result['status'] == 'partial_scenario'
    assert result['expected_packets'] == result['unretrieved_packets'] == 1
    assert result['retrieved_packets'] == 0
    node = result['nodes'][0]
    assert node['public_key'] == B
    expected = lora_airtime_ms(2+4+len(bytes.fromhex(packet['payload_hex'])), **MOD)
    assert result['additional_airtime_ms'] == pytest.approx(expected, abs=.001)
    assert node['scenario_hour_percent'] == pytest.approx(expected/3600000*100, abs=1e-6)
    assert node['reconstructed_hour_percent'] is None
    assert result['is_fdr'] is False and node['compliance'] == 'unknown'
    assert result['local_load']['scenario_percent'] is None  # B has no direct anchor here.


def test_retrieved_forward_does_not_add_a_second_emission(db):
    train(db)
    add(db, END-150, [A], nonce=1)
    add(db, END-120, [A, B], nonce=1)
    result = model(db, events={B: [(END-120, 1000, True)]}, local_keys={B})
    assert result['expected_packets'] == result['retrieved_packets'] == 1
    assert result['unretrieved_packets'] == 0
    assert result['independent_exposure_packets'] == 1
    assert result['additional_airtime_ms'] == 0
    assert result['nodes'][0]['scenario_hour_percent'] == pytest.approx(1000/3600000*100, abs=1e-6)


def test_a_path_through_the_target_is_not_independent_exposure(db):
    train(db);add(db, END-120, [A, B])
    result = model(db)
    assert result['expected_packets'] == result['retrieved_packets'] == 1
    assert result['independent_exposure_packets'] == 0
    assert result['is_fdr'] is False


def test_multiple_upstreams_and_copies_predict_one_forward_per_original(db):
    train(db)
    for i, age in enumerate([3000, 2000, 1000]):
        add(db, START-age, [D, B], nonce=200+i)
    for path in [[A], [D], [A]]:
        add(db, END-120, path, nonce=1)
    result = model(db)
    assert result['evaluation']['distinct_packets'] == 1
    assert result['expected_packets'] == result['unretrieved_packets'] == 1


def test_only_locally_anchored_extra_emissions_change_local_scenario(db):
    train(db)
    add(db, END-120, [A])
    result = model(db, local_keys={B}, events={B: [(END-200, 1000, True)]})
    assert result['local_load']['additional_airtime_ms'] == result['additional_airtime_ms']
    assert result['local_load']['scenario_percent'] == pytest.approx((1000+result['additional_airtime_ms'])/3600000*100, abs=1e-6)


def test_depth_beyond_positive_envelope_is_unknown_not_a_guessed_limit(db):
    train(db)
    add(db, END-120, [D, A])
    result = model(db)
    assert result['expected_packets'] == 0
    assert result['excluded']['unsupported_hop_depth'] == 1
    assert result['additional_airtime_ms'] is None


def test_single_deep_outlier_does_not_expand_supported_hop_depth(db):
    train(db)
    add(db, START-500, [D, A, B], nonce=300)
    add(db, END-120, [D, A])
    result = model(db)
    assert result['expected_packets'] == 0


@pytest.mark.parametrize('change', ['advert', 'scope', 'short', 'unknown_origin', 'loop', 'observer', 'origin', 'profile'])
def test_ineligible_frames_are_not_silently_modelled(db, change):
    train(db)
    kwargs = {'kind': 'ADVERT'} if change == 'advert' else {'scope': '#region-two'} if change == 'scope' else {'bph': 1} if change == 'short' else {}
    add(db, END-120, [A, A] if change == 'loop' else [A], **kwargs)
    if change == 'profile':
        while ingest_stream(db, MOD, scope_keys=KEYS)['processed']:pass
        db.execute('UPDATE airtime_observations SET modulation_json=? WHERE observed_at>=?',
                   (json.dumps({**MOD, 'cr_den': 5}, sort_keys=True), START))
    result = model(db, author=None if change == 'unknown_origin' else B if change == 'origin' else C,
                   observer=B if change == 'observer' else None)
    assert result['expected_packets'] == 0
    assert result['additional_airtime_ms'] is None


def test_scopes_are_resolved_per_payload_not_by_raw_transport_code(db):
    train(db, scope='#region-one')
    p1 = add(db, END-200, [A], nonce=1, scope='#region-one')
    p2 = add(db, END-120, [A], nonce=2, scope='#region-one')
    assert p1['transport_codes']['code1'] != p2['transport_codes']['code1']
    result = model(db)
    assert result['expected_packets'] == 2
    contexts = db.execute("SELECT DISTINCT scope_id FROM airtime_packet_context WHERE scope_id IS NOT NULL").fetchall()
    assert len(contexts) == 1
    stored = repr(db.execute('SELECT * FROM airtime_packet_context').fetchall())
    assert '#region-one' not in stored and KEYS['#region-one'].hex() not in stored
    assert 'private-encrypted-payload' not in stored and p1['payload_hex'] not in stored


def test_unknown_scope_can_be_backfilled_when_keys_become_available(db):
    train(db, scope='#region-one')
    add(db, END-120, [A], scope='#region-one')
    assert model(db, keys={})['expected_packets'] == 0
    assert model(db)['expected_packets'] == 1
    assert model(db)['expected_packets'] == 1  # Idempotent independent watermark.


def test_newly_discovered_scope_collision_clears_previous_match(db, monkeypatch):
    packet = add(db, END-120, [A], scope='#region-one')
    code = packet['transport_codes']['code1'].to_bytes(2, 'little')
    monkeypatch.setattr('modules.airtime_forwarding.hmac.new', lambda *args: SimpleNamespace(digest=lambda: code+b'\0'*30))
    model(db, keys={'#region-one': KEYS['#region-one']})
    assert db.execute('SELECT scope_id FROM airtime_packet_context').fetchone()[0] is not None
    model(db, keys=KEYS)
    assert db.execute('SELECT scope_id,scope_status FROM airtime_packet_context').fetchone() == (None, 'ambiguous_scope')


def test_training_must_precede_the_evaluation_and_have_distinct_payloads(db):
    for at in [END-3000, END-2000, END-1000]:add(db, at, [A, B], nonce=int(at))
    assert model(db)['status'] == 'insufficient_training'
    db.execute('DELETE FROM packet_stream');db.execute('DELETE FROM airtime_observations')
    db.execute('DELETE FROM airtime_packet_context');db.execute('DELETE FROM airtime_ingest_state');db.execute('DELETE FROM airtime_context_state')
    for age in [30000, 2000, 1000]:add(db, START-age, [A, B], nonce=100)
    add(db, END-120, [A], nonce=1)
    assert model(db)['expected_packets'] == 0


def test_old_links_and_forwarding_profiles_expire(db):
    for i in range(3):add(db, START-2*86400-i*100, [A, B], nonce=100+i)
    add(db, END-120, [A])
    result = model(db)
    assert result['expected_packets'] == 0
    assert result['excluded']['stale_training'] == 1


def test_recent_packets_wait_for_returns(db):
    train(db);add(db, END-10, [A])
    result = model(db)
    assert result['expected_packets'] == 0
    assert result['excluded']['awaiting_returns'] == 1


def test_hypothetical_transmission_does_not_trigger_another_hop(db):
    train(db)
    for i, age in enumerate([3000, 2000, 1000]):add(db, START-age, [A, B, D], nonce=200+i)
    add(db, END-120, [A])
    result = model(db)
    assert {node['public_key'] for node in result['nodes']} == {B}


def test_scope_matching_rejects_invalid_inputs():
    assert packet_scope({'route_type_name': 'TRANSPORT_FLOOD'}, KEYS) == (None, 'unknown_scope')
    assert packet_scope({'route_type_name': 'DIRECT'}, KEYS) == (None, 'not_flood')


def test_old_predictions_do_not_claim_a_current_hour_scenario(db):
    train(db);add(db, END-120, [A])
    while ingest_stream(db, MOD, scope_keys=KEYS)['processed']:pass
    authors = {fp: C for fp, in db.execute('SELECT fingerprint FROM airtime_observations WHERE observed_at>=?', (START,))}
    result = build_forwarding_model(db, Contacts(db), start=START, end=END+7200, authors=authors,
        evidence_events={B: [(END+7100, 1000, True)]}, local_keys={B}, local_airtime_ms=1000,
        available_seconds=10800)
    node = result['nodes'][0]
    assert node['expected_packets'] == 1
    assert node['reconstructed_hour_percent'] > 0
    assert node['last_hour_expected_packets'] == 0
    assert node['scenario_hour_percent'] is None
    assert node['scenario_hour_status'] == 'no_supported_hourly_opportunity'


def test_analysis_uses_existing_outgoing_regions_without_changing_allowlist():
    config = configparser.ConfigParser()
    config.read_dict({'Channels': {'flood_scopes': '*', 'startup_flood_scope': 'region-one',
                                  'outgoing_flood_scope_override': '#region-two', 'password': 'secret'}})
    before = dict(config.items('Channels'))
    keys = analysis_scope_keys(config)
    assert set(keys) == {'#region-one', '#region-two'}
    assert keys['#region-one'] == hashlib.sha256(b'#region-one').digest()[:16]
    assert dict(config.items('Channels')) == before
