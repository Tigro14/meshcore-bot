"""Passive, metadata-only RF airtime attribution; schema-versioned read model.

RX is local hearing. Relay TX is reconstructed from flood history, never direct
route instructions or TRACE SNR bytes. No inference proves duty-cycle compliance.
Raw payloads are consumed for length/fingerprint/advert identity, never persisted.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import time
from collections import defaultdict
from typing import Any

from modules.channel_occupancy import lora_airtime_ms

SCHEMA_VERSION = 1
METHOD_VERSION = 3
DEDUP_SECONDS = 60
MAX_ROWS = 100000
WINDOWS = {'1h': 3600, '24h': 86400, '7d': 604800}
REFERENCE_PERCENT = 10.0


def _hex(value: Any, lengths: tuple[int, ...] = ()) -> str | None:
    if not isinstance(value, str) or not value or len(value) % 2:
        return None
    try:
        bytes.fromhex(value)
    except ValueError:
        return None
    if lengths and len(value) not in lengths:
        return None
    return value.lower()


def create_tables(cursor: sqlite3.Cursor) -> None:
    schema = '''
        CREATE TABLE IF NOT EXISTS airtime_observations (
            source_id INTEGER PRIMARY KEY, observed_at REAL NOT NULL,
            fingerprint TEXT NOT NULL, packet_hash TEXT, route TEXT NOT NULL,
            payload_type TEXT NOT NULL, frame_bytes INTEGER NOT NULL,
            payload_bytes INTEGER NOT NULL, path_json TEXT NOT NULL,
            bytes_per_hop INTEGER NOT NULL, airtime_ms REAL NOT NULL,
            modulation_json TEXT NOT NULL, author_key TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_airtime_observed_at ON airtime_observations(observed_at);
        CREATE TABLE IF NOT EXISTS airtime_author_links (
            source_id INTEGER PRIMARY KEY, observed_at REAL NOT NULL,
            packet_hash TEXT NOT NULL, author_key TEXT, author_name TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_airtime_author_time ON airtime_author_links(observed_at);
        CREATE TABLE IF NOT EXISTS airtime_ingest_state (
            id INTEGER PRIMARY KEY CHECK(id=1), last_source_id INTEGER NOT NULL,
            updated_at REAL NOT NULL
        );
    '''
    for statement in schema.split(';'):
        if statement.strip():
            cursor.execute(statement)


def normalize_packet(packet: dict, observed_at: float, source_id: int, modulation: dict) -> tuple | None:
    """Reject inconsistent frames; only actual decoded RF packet metadata enters RX."""
    payload = _hex(packet.get('payload_hex'))
    if payload is None:
        return None
    payload_bytes = len(payload) // 2
    if not payload_bytes:
        return None
    route = str(packet.get('route_type_name') or '').upper()
    packet_type = str(packet.get('payload_type_name') or 'UNKNOWN').upper()
    if route not in {'FLOOD', 'TRANSPORT_FLOOD', 'DIRECT', 'TRANSPORT_DIRECT'}:
        return None
    try:
        bph = int(packet.get('bytes_per_hop') or 1)
        path_hex = str(packet.get('path_hex') or '').lower()
        if bph not in (1, 2, 3) or (path_hex and _hex(path_hex) is None):
            return None
        if len(path_hex) % (2*bph) or len(path_hex) > 128:
            return None
        path = [path_hex[i:i+2*bph] for i in range(0, len(path_hex), 2*bph)]
        frame_bytes = 2 + (4 if route.startswith('TRANSPORT_') else 0) + len(path_hex)//2 + payload_bytes
        raw = _hex(packet.get('raw_packet_hex'))
        if raw is not None and len(raw)//2 != frame_bytes:
            return None
        airtime = lora_airtime_ms(frame_bytes, **modulation)
        if not math.isfinite(airtime) or airtime <= 0:
            return None
    except (ValueError, TypeError, OverflowError):
        return None
    author = payload[:64] if packet_type == 'ADVERT' and payload_bytes >= 100 else None
    fingerprint = hashlib.sha256((packet_type + ':' + payload).encode()).hexdigest()
    return (source_id, observed_at, fingerprint, _hex(packet.get('packet_hash')), route,
            packet_type, frame_bytes, payload_bytes, json.dumps(path), bph, airtime,
            json.dumps(modulation, sort_keys=True), author)


def ingest_stream(conn: sqlite3.Connection, modulation: dict, *, limit: int = 1000) -> dict:
    """Idempotent incremental import from packet_stream, in the existing writer thread.

    No message text, channel name, payload or raw frame is copied. Correlation uses
    a packet fingerprint rather than message text or time-based sender guessing.
    """
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='airtime_observations'").fetchone():
        return {'processed': 0, 'imported': 0}
    state = conn.execute('SELECT last_source_id FROM airtime_ingest_state WHERE id=1').fetchone()
    last_id = state[0] if state else 0
    rows = conn.execute('SELECT id,timestamp,data,type FROM packet_stream WHERE id>? ORDER BY id LIMIT ?',
                        (last_id, limit)).fetchall()
    imported = 0
    for source_id, observed_at, raw, kind in rows:
        last_id = source_id
        if kind not in ('packet', 'message'):
            continue
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                continue
            if kind == 'packet':
                values = normalize_packet(data, observed_at, source_id, modulation)
                if values:
                    conn.execute('INSERT OR IGNORE INTO airtime_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', values)
                    imported += 1
            else:
                packet_hash = _hex(data.get('packet_hash'))
                if packet_hash:
                    key = _hex(data.get('sender_pubkey'), (64,))
                    name = data.get('sender')
                    name = name[:128] if isinstance(name, str) and name.strip() else None
                    conn.execute('INSERT OR IGNORE INTO airtime_author_links VALUES (?,?,?,?,?)',
                                 (source_id, observed_at, packet_hash, key, name))
        except (ValueError, TypeError, OverflowError):
            continue
    if rows:
        conn.execute('INSERT INTO airtime_ingest_state VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET '
                     'last_source_id=excluded.last_source_id,updated_at=excluded.updated_at', (last_id, time.time()))
    return {'processed': len(rows), 'imported': imported}


class Contacts:
    def __init__(self, conn: sqlite3.Connection):
        self.nodes: dict[str, dict] = {}
        self.names: dict[str, list[str]] = defaultdict(list)
        self.prefixes: dict[str, list[str]] = defaultdict(list)
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='complete_contact_tracking'").fetchone():
            return
        for key, name, role in conn.execute('SELECT public_key,name,role FROM complete_contact_tracking'):
            key = _hex(key, (64,))
            if not key:
                continue
            self.nodes[key] = {'public_key': key, 'name': name or key[:12], 'role': str(role or '').lower()}
            if name:
                self.names[name].append(key)
            for size in (2, 4, 6):
                self.prefixes[key[:size]].append(key)

    def resolve(self, prefix: str) -> tuple[str | None, str]:
        matches = self.prefixes.get(prefix.lower(), [])
        return (matches[0], 'unique_known_prefix') if len(matches) == 1 else (None, 'ambiguous' if matches else 'unknown')

    def author(self, key: str | None, name: str | None) -> str | None:
        if key:
            return key
        matches = self.names.get(name or '', [])
        return matches[0] if len(matches) == 1 else None

    def relay(self, key: str) -> bool:
        return self.nodes.get(key, {}).get('role') in ('repeater', 'roomserver', 'room server')

    def identity(self, key: str) -> dict:
        return self.nodes.get(key, {'public_key': key, 'name': key[:12], 'role': 'unknown'})


def _category(packet_type: str) -> str:
    return 'adverts' if packet_type == 'ADVERT' else 'messages' if packet_type in ('GRP_TXT', 'TXT_MSG') else 'other'


def duty_summary(events: list[tuple[float, float, bool]], start: float, end: float) -> dict:
    """Exact (t-3600, t] windows over event timestamps, including the left lookbehind.

    Direct events use reception times; inferred event times remain estimates.
    There is no minute bucketing and missing observation is never compliance.
    """
    selected = [event for event in events if start <= event[0] <= end]
    if not selected:
        return {'estimated_percent': None, 'last_hour_percent': None, 'peak_hour_percent': None,
                'peak_hour_ending_at': None,
                'airtime_ms': 0.0, 'direct_airtime_ms': 0.0, 'inferred_airtime_ms': 0.0,
                'packets': 0, 'reference_percent': REFERENCE_PERCENT, 'compliance': 'unknown',
                'status': 'no_attributed_observation'}
    total = sum(event[1] for event in selected)
    ordered = sorted((at, ms) for at, ms, _ in events if start-3600 < at <= end)
    # A peak can already exist at the start, before the next selected event.
    peak = sum(ms for at, ms in ordered if at <= start)
    peak_at = start
    rolling = 0.0
    left = 0
    for right, (at, ms) in enumerate(ordered):
        rolling += ms
        while left <= right and ordered[left][0] <= at-3600:
            rolling -= ordered[left][1]
            left += 1
        if at >= start and rolling > peak:
            peak = rolling
            peak_at = at
    last = sum(ms for at, ms in ordered if end-3600 < at)
    peak_percent = peak/3600000*100
    return {'estimated_percent': round(total/max(1,end-start)/1000*100, 6),
            'last_hour_percent': round(last/3600000*100, 6),
            'peak_hour_percent': round(peak_percent, 6), 'peak_hour_ending_at': peak_at,
            'airtime_ms': round(total, 3),
            'direct_airtime_ms': round(sum(ms for _, ms, direct in selected if direct), 3),
            'inferred_airtime_ms': round(sum(ms for _, ms, direct in selected if not direct), 3),
            'packets': len(selected), 'reference_percent': REFERENCE_PERCENT, 'compliance': 'unknown',
            'status': 'reference_reached_in_estimate' if peak_percent >= REFERENCE_PERCENT else 'partial_estimate',
            'first_observed_at': min(at for at, _, _ in selected),
            'last_observed_at': max(at for at, _, _ in selected)}


def _reconstruct_groups(evidence: list[tuple[float, float, bool, float]],
                        *, count_since: float = -math.inf) -> tuple[list[dict[str, Any]], int]:
    """Match compatible observations of one relay/path, without swallowing later TX.

    Evidence is (received_at, hop_airtime_ms, direct, downstream_airtime_seconds).
    A downstream copy cannot predate its upstream transmission plus the airtime
    of the remaining hops. For an inferred event, intersect those causal intervals
    with a 60-second allowance for unobserved forwarding delays. That allowance
    is a modelling assumption, not a known firmware delay limit.

    Every directly received emission is retained. A downstream observation can
    match at most one compatible group; two direct anchors never merge. A later
    direct reception cannot retrospectively absorb an earlier downstream copy.
    Unresolved retries at intermediate hops can still remain merged.
    """
    groups: list[dict[str, Any]] = []
    collapsed = 0
    for at, ms, direct, downstream_seconds in sorted(evidence):
        if direct:
            groups.append({'lower': at, 'upper': at, 'ms': ms, 'direct': True,
                           'observed_in_period': at >= count_since})
            continue
        latest = at-downstream_seconds
        earliest = latest-DEDUP_SECONDS
        match = next((group for group in reversed(groups)
                      if max(group['lower'], earliest) <= min(group['upper'], latest)), None)
        if match is None:
            groups.append({'lower': earliest, 'upper': latest, 'ms': ms, 'direct': False,
                           'observed_in_period': at >= count_since})
        else:
            match['lower'] = max(match['lower'], earliest)
            match['upper'] = min(match['upper'], latest)
            match['observed_in_period'] |= at >= count_since
            collapsed += int(at >= count_since)
    return groups, collapsed


def _reconstruct_emissions(evidence: list[tuple[float, float, bool, float]],
                           *, count_since: float = -math.inf) -> tuple[list[tuple[float, float, bool]], int]:
    groups, collapsed = _reconstruct_groups(evidence, count_since=count_since)
    return [(group['upper'], group['ms'], group['direct']) for group in groups], collapsed


def _traffic_summary(packets: dict[str, dict], costs: dict[str, dict], contacts: Contacts) -> tuple[dict, list]:
    """One original packet per fingerprint, independent of received-copy multiplicity.

    Radio cost sums reconstructed source + relay emission groups supported by
    selected receptions. Unknown hop identities still have a modelled wire length;
    their cost is counted and explicitly marked unresolved, never assigned a node.
    Direct routing/TRACE cannot supply that reconstruction and remain unavailable.
    """
    def empty_author(identity: dict) -> dict:
        return {**identity, 'distinct_packets': 0, 'received_packets': 0, 'rx_airtime_ms': 0.0,
                'reconstructed_radio_airtime_ms': None, 'cost_reconstructed_packets': 0,
                'identification_methods': [], 'categories': {
                    kind: {'distinct_packets': 0, 'packets': 0, 'airtime_ms': 0.0,
                           'reconstructed_radio_airtime_ms': None} for kind in ('messages', 'adverts', 'other')}}

    authors: dict[str, dict] = {}
    unknown = empty_author({'public_key': None, 'name': 'Auteur non identifiable', 'role': 'unknown'})
    traffic: dict[str, Any] = {
        'distinct_packets': len(packets), 'received_packets': 0, 'additional_received_copies': 0,
        'identified_author_packets': 0, 'unattributed_author_packets': 0,
        'reconstructed_radio_airtime_ms': None, 'origin_airtime_ms': 0.0, 'relay_airtime_ms': 0.0,
        'unresolved_relay_airtime_ms': 0.0, 'cost_reconstructed_packets': 0,
        'cost_unreconstructed_packets': 0, 'path_packets': 0, 'identifiable_path_packets': 0,
        'categories': {kind: {'distinct_packets': 0} for kind in ('messages', 'adverts', 'other')},
    }
    for fingerprint, packet in packets.items():
        author = packet['author_key']
        item = authors.setdefault(author, empty_author(contacts.identity(author))) if author else unknown
        category = _category(packet['kind'])
        item['distinct_packets'] += 1
        item['received_packets'] += packet['received_packets']
        item['rx_airtime_ms'] += packet['rx_airtime_ms']
        for method in packet['identification_methods']:
            if method not in item['identification_methods']:
                item['identification_methods'].append(method)
        item['categories'][category]['distinct_packets'] += 1
        item['categories'][category]['packets'] += packet['received_packets']
        item['categories'][category]['airtime_ms'] += packet['rx_airtime_ms']
        traffic['received_packets'] += packet['received_packets']
        traffic['categories'][category]['distinct_packets'] += 1
        cost = costs.get(fingerprint)
        if cost:
            total = cost['origin_airtime_ms'] + cost['relay_airtime_ms']
            item['reconstructed_radio_airtime_ms'] = (item['reconstructed_radio_airtime_ms'] or 0.0) + total
            item['cost_reconstructed_packets'] += 1
            cat = item['categories'][category]
            cat['reconstructed_radio_airtime_ms'] = (cat['reconstructed_radio_airtime_ms'] or 0.0) + total
            traffic['reconstructed_radio_airtime_ms'] = (traffic['reconstructed_radio_airtime_ms'] or 0.0) + total
            traffic['cost_reconstructed_packets'] += 1
            for key in ('origin_airtime_ms', 'relay_airtime_ms', 'unresolved_relay_airtime_ms'):
                traffic[key] += cost[key]
    traffic['additional_received_copies'] = traffic['received_packets'] - len(packets)
    traffic['unattributed_author_packets'] = unknown['distinct_packets']
    traffic['identified_author_packets'] = len(packets) - unknown['distinct_packets']
    traffic['cost_unreconstructed_packets'] = len(packets) - traffic['cost_reconstructed_packets']
    traffic['unknown_author'] = unknown
    return traffic, sorted(authors.values(), key=lambda item: (
        item['distinct_packets'], item['reconstructed_radio_airtime_ms'] or 0.0), reverse=True)


def build_report(conn: sqlite3.Connection, *, now: float | None = None, window: str = '24h',
                 _comparison: bool = True, _end_exclusive: bool = False) -> dict:
    now = time.time() if now is None else now
    seconds = WINDOWS.get(window, 86400)
    start = now-seconds
    contacts = Contacts(conn)
    report: dict[str, Any] = {
        'schema_version': SCHEMA_VERSION, 'generated_at': now, 'window': window,
        'start_time': start, 'end_time': now, 'window_seconds': seconds,
        'units': {'airtime': 'milliseconds', 'timestamp': 'unix_seconds', 'percent': '0_to_100'},
        'method': {'version': METHOD_VERSION, 'airtime': 'lora_full_frame_model', 'relay': 'flood_path_reconstruction',
                   'branch_dedup_seconds': DEDUP_SECONDS, 'branch_dedup': 'causal_path_intervals',
                   'inferred_time': 'latest_compatible_upstream_time',
                   'peak_window': 'event_timestamps', 'peak_resolution_seconds': None,
                   'traffic_identity': 'payload_fingerprint', 'duty_reference_scope': 'per_transmitter',
                   'traffic_cost_basis': 'emission_groups_supported_by_selected_receptions',
                   'participation_denominator': 'distinct_flood_packets_with_nonempty_received_path',
                   'compliance': 'not_determined',
                   'rx_denominator': 'available_wall_clock_span', 'modulation_profiles': []},
        'limitations': ['local_receiver_only', 'unheard_transmissions_not_counted',
                        'known_prefix_uniqueness_not_identity_proof', 'same_path_retries_may_be_merged_when_inferred',
                        'unobserved_forwarding_delay_is_assumed', 'inferred_transmission_time_is_estimated',
                        'no_direct_route_or_trace_inference', 'historical_modulation_assumed_during_backfill',
                        'winning_paths_do_not_describe_all_flood_branches', 'radio_cost_is_not_global_channel_occupancy'],
        'coverage': {'first_observation_at': None, 'last_observation_at': None,
                     'missing_intervals_are_unknown': True, 'truncated': False,
                     'capture_uptime_known': False, 'effective_start_time': start,
                     'available_seconds': seconds},
        'local': {'rx_airtime_ms': 0.0, 'rx_packets': 0, 'rx_percent': None,
                  'unattributed_author_airtime_ms': 0.0, 'unattributed_author_packets': 0},
        'quality': {'ambiguous_hops': 0, 'unknown_hops': 0, 'excluded_route_packets': 0,
                    'collapsed_inferred_copies': 0}, 'authors': [], 'repeaters': [],
        'comparison': {'status': 'insufficient_history', 'previous_start_time': start-seconds,
                       'previous_end_time': start, 'window_seconds': seconds,
                       'capture_uptime_known': False, 'interpretation': 'observed_traffic_only'}}
    report['traffic'], _ = _traffic_summary({}, {}, contacts)
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='airtime_observations'").fetchone():
        return report
    retained_from = conn.execute('SELECT MIN(observed_at) FROM airtime_observations').fetchone()[0]
    if retained_from is not None:
        report['coverage']['effective_start_time'] = max(start, retained_from)
        report['coverage']['available_seconds'] = max(1.0, now-max(start, retained_from))
    columns = 'observed_at,fingerprint,packet_hash,route,payload_type,frame_bytes,payload_bytes,path_json,bytes_per_hop,airtime_ms,modulation_json,author_key'
    end_operator = '<' if _end_exclusive else '<='
    rows = conn.execute(f'SELECT {columns} FROM airtime_observations WHERE observed_at>=? AND observed_at{end_operator}? '
                        'ORDER BY observed_at DESC,source_id DESC LIMIT ?', (start-3600-DEDUP_SECONDS, now, MAX_ROWS+1)).fetchall()
    report['coverage']['truncated'] = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS][::-1]
    links: dict[str, list] = defaultdict(list)
    for at, packet_hash, key, name in conn.execute('SELECT observed_at,packet_hash,author_key,author_name '
                                                 'FROM airtime_author_links WHERE observed_at>=? AND observed_at<=?',
                                                 (start-3600-DEDUP_SECONDS, now+DEDUP_SECONDS)):
        key = contacts.author(key, name)
        if key:
            links[packet_hash].append((at, key))
    # A strong fingerprint lets later copies identify the same original packet,
    # but conflicting identities stay unknown rather than being split into authors.
    author_keys: dict[str, set[str]] = defaultdict(set)
    author_methods: dict[str, set[str]] = defaultdict(set)
    for at, fingerprint, packet_hash, _, _, _, _, _, _, _, _, author in rows:
        if author:
            author_keys[fingerprint].add(author)
            author_methods[fingerprint].add('advertisement_public_key')
        elif packet_hash:
            author_candidates = {key for when, key in links.get(packet_hash, []) if abs(at-when) <= DEDUP_SECONDS}
            author_keys[fingerprint].update(author_candidates)
            if author_candidates:
                author_methods[fingerprint].add('correlated_decoded_message')
    resolved_authors = {fp: next(iter(keys)) if len(keys) == 1 else None for fp, keys in author_keys.items()}
    packets: dict[str, dict] = {}
    path_packets: set[str] = set()
    identified_path_packets: set[str] = set()
    participation: dict[str, set[str]] = defaultdict(set)
    # Different wire formats / radio profiles cannot be the same physical TX.
    evidence: dict[tuple, list[tuple[float, float, bool, float]]] = defaultdict(list)
    targets: dict[tuple, str | None] = {}
    for at, fingerprint, packet_hash, route, kind, frame_bytes, payload_bytes, path_json, bph, ms, modulation_json, author in rows:
        path = json.loads(path_json)
        author = resolved_authors.get(fingerprint)
        if at >= start:
            local = report['local']
            profile = json.loads(modulation_json)
            if profile not in report['method']['modulation_profiles']:
                report['method']['modulation_profiles'].append(profile)
            local['rx_airtime_ms'] += ms
            local['rx_packets'] += 1
            if report['coverage']['first_observation_at'] is None:
                report['coverage']['first_observation_at'] = at
            report['coverage']['last_observation_at'] = at
            packet = packets.setdefault(fingerprint, {'author_key': author, 'kind': kind,
                'received_packets': 0, 'rx_airtime_ms': 0.0,
                'identification_methods': sorted(author_methods[fingerprint]) if author else []})
            packet['received_packets'] += 1
            packet['rx_airtime_ms'] += ms
            if not author:
                local['unattributed_author_airtime_ms'] += ms
                local['unattributed_author_packets'] += 1
        if route not in ('FLOOD', 'TRANSPORT_FLOOD') or kind == 'TRACE':
            if at >= start:
                report['quality']['excluded_route_packets'] += 1
            continue
        modulation = json.loads(modulation_json)
        # Reception is timestamped at the end of the frame. An upstream RX-end
        # precedes it by at least the complete airtime of every downstream frame.
        base_size = 2 + (4 if route.startswith('TRANSPORT_') else 0) + payload_bytes
        stage_ms = [lora_airtime_ms(base_size+count*bph, **modulation)
                    for count in range(len(path)+1)]
        downstream_ms = [0.0] * len(stage_ms)
        for count in range(len(path)-1, -1, -1):
            downstream_ms[count] = downstream_ms[count+1] + stage_ms[count+1]
        candidates: list[tuple[str | None, tuple[str, ...], bool, int]] = [
            (author if author and contacts.relay(author) else None, (), len(path) == 0, 0)]
        if path and at >= start:
            path_packets.add(fingerprint)
        for index, prefix in enumerate(path):
            key, status = contacts.resolve(prefix)
            if not key or not contacts.relay(key):
                if at >= start:
                    report['quality']['ambiguous_hops' if status == 'ambiguous' else 'unknown_hops'] += 1
                key = None
            elif at >= start:
                participation[key].add(fingerprint)
                identified_path_packets.add(fingerprint)
            candidates.append((key, tuple(path[:index+1]), index == len(path)-1, (index+1)*bph))
        for key, chain, direct, path_bytes in candidates:
            stage = path_bytes//bph
            hop_ms = ms if direct else stage_ms[stage]
            identity = (fingerprint, chain, route, bph, modulation_json)
            targets[identity] = key
            evidence[identity].append((at, hop_ms, direct, downstream_ms[stage]/1000))
    events: dict[str, list[tuple[float, float, bool]]] = defaultdict(list)
    costs: dict[str, dict] = {}
    for identity, observations in evidence.items():
        fingerprint, chain, *_ = identity
        groups, collapsed = _reconstruct_groups(observations, count_since=start)
        key = targets[identity]
        if key:
            events[key].extend((group['upper'], group['ms'], group['direct']) for group in groups)
            report['quality']['collapsed_inferred_copies'] += collapsed
        for group in groups:
            if group['observed_in_period']:
                cost = costs.setdefault(fingerprint, {'origin_airtime_ms': 0.0, 'relay_airtime_ms': 0.0,
                                                       'unresolved_relay_airtime_ms': 0.0})
                cost['relay_airtime_ms' if chain else 'origin_airtime_ms'] += group['ms']
                if chain and not key:
                    cost['unresolved_relay_airtime_ms'] += group['ms']
    report['traffic'], report['authors'] = _traffic_summary(packets, costs, contacts)
    report['traffic']['path_packets'] = len(path_packets)
    report['traffic']['identifiable_path_packets'] = len(identified_path_packets)
    for key, relay_events in events.items():
        duty = duty_summary(relay_events, start, now)
        if duty['packets']:
            report['repeaters'].append({**contacts.identity(key), **duty,
                                        'path_packets': len(participation[key]),
                                        'path_packets_total': len(path_packets),
                                        'participation_percent': round(len(participation[key])/len(path_packets)*100, 6) if path_packets else None,
                                        'trend': {'status': 'insufficient_history', 'change_percent': None,
                                                  'previous_airtime_ms': None},
                                        'identification': 'unique_among_known_nodes', 'hardware_tx_counter': False})
    report['repeaters'].sort(key=lambda item: (item['path_packets'], item['airtime_ms']), reverse=True)
    local = report['local']
    if local['rx_packets']:
        local['requested_window_rx_percent'] = round(local['rx_airtime_ms']/1000/seconds*100, 6)
        local['rx_percent'] = round(local['rx_airtime_ms']/1000/report['coverage']['available_seconds']*100, 6)
    if _comparison and retained_from is not None and retained_from <= start-seconds:
        if report['coverage']['truncated']:
            report['comparison']['status'] = 'truncated'
        else:
            previous = build_report(conn, now=start, window=window, _comparison=False, _end_exclusive=True)
            if previous['coverage']['truncated']:
                report['comparison']['status'] = 'truncated'
            else:
                report['comparison']['status'] = 'available_partial_observations'
                previous_nodes = {node['public_key']: node for node in previous['repeaters']}
                for node in report['repeaters']:
                    before = previous_nodes.get(node['public_key'])
                    node['trend'] = {'status': 'no_previous_observation', 'change_percent': None,
                                     'previous_airtime_ms': None}
                    if before and before['airtime_ms'] > 0:
                        node['trend'] = {'status': 'partial_observations',
                            'change_percent': round((node['airtime_ms']/before['airtime_ms']-1)*100, 3),
                            'previous_airtime_ms': before['airtime_ms']}
    return report


class AirtimeReader:
    """Bounded, short-lived cache shared by Airtime and Contacts in one viewer."""
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, dict]] = {}

    def read(self, conn: sqlite3.Connection, window: str = '24h') -> dict:
        window = window if window in WINDOWS else '24h'
        with self._lock:
            current = time.time()
            cached = self._cache.get(window)
            if cached and current-cached[0] < 15:
                return cached[1]
            result = build_report(conn, now=current, window=window)
            self._cache[window] = (current, result)
            return result
