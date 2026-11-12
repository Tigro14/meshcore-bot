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
    """Rolling-hour maximum at minute precision; missing observation is never compliance."""
    selected = [event for event in events if start <= event[0] <= end]
    if not selected:
        return {'estimated_percent': None, 'last_hour_percent': None, 'peak_hour_percent': None,
                'airtime_ms': 0.0, 'direct_airtime_ms': 0.0, 'inferred_airtime_ms': 0.0,
                'packets': 0, 'reference_percent': REFERENCE_PERCENT, 'compliance': 'unknown',
                'status': 'no_attributed_observation'}
    total = sum(event[1] for event in selected)
    hourly: dict[int, float] = defaultdict(float)
    for at, ms, _ in events:
        if start-3600 <= at <= end:
            hourly[int(at//60)] += ms
    rolling = 0.0
    peak = 0.0
    for minute in range(int(start//60), int(end//60)+1):
        rolling += hourly.get(minute, 0.0)
        if minute == int(start//60):
            rolling += sum(hourly.get(i, 0.0) for i in range(minute-59, minute))
        else:
            rolling -= hourly.get(minute-60, 0.0)
        peak = max(peak, rolling)
    last = sum(ms for at, ms, _ in events if end-3600 < at <= end)
    peak_percent = peak/3600000*100
    return {'estimated_percent': round(total/max(1,end-start)/1000*100, 6),
            'last_hour_percent': round(last/3600000*100, 6),
            'peak_hour_percent': round(peak_percent, 6), 'airtime_ms': round(total, 3),
            'direct_airtime_ms': round(sum(ms for _, ms, direct in selected if direct), 3),
            'inferred_airtime_ms': round(sum(ms for _, ms, direct in selected if not direct), 3),
            'packets': len(selected), 'reference_percent': REFERENCE_PERCENT, 'compliance': 'unknown',
            'status': 'reference_reached_in_estimate' if peak_percent >= REFERENCE_PERCENT else 'partial_estimate',
            'first_observed_at': min(at for at, _, _ in selected),
            'last_observed_at': max(at for at, _, _ in selected)}


def build_report(conn: sqlite3.Connection, *, now: float | None = None, window: str = '24h') -> dict:
    now = time.time() if now is None else now
    seconds = WINDOWS.get(window, 86400)
    start = now-seconds
    contacts = Contacts(conn)
    report: dict[str, Any] = {
        'schema_version': SCHEMA_VERSION, 'generated_at': now, 'window': window,
        'start_time': start, 'end_time': now, 'window_seconds': seconds,
        'units': {'airtime': 'milliseconds', 'timestamp': 'unix_seconds', 'percent': '0_to_100'},
        'method': {'airtime': 'lora_full_frame_model', 'relay': 'flood_path_reconstruction',
                   'branch_dedup_seconds': DEDUP_SECONDS, 'peak_resolution_seconds': 60,
                   'reference_percent': REFERENCE_PERCENT, 'compliance': 'not_determined',
                   'rx_denominator': 'available_wall_clock_span', 'modulation_profiles': []},
        'limitations': ['local_receiver_only', 'unheard_transmissions_not_counted',
                        'known_prefix_uniqueness_not_identity_proof', 'same_path_retries_may_be_merged_when_inferred',
                        'no_direct_route_or_trace_inference', 'historical_modulation_assumed_during_backfill'],
        'coverage': {'first_observation_at': None, 'last_observation_at': None,
                     'missing_intervals_are_unknown': True, 'truncated': False,
                     'capture_uptime_known': False, 'effective_start_time': start,
                     'available_seconds': seconds},
        'local': {'rx_airtime_ms': 0.0, 'rx_packets': 0, 'rx_percent': None,
                  'unattributed_author_airtime_ms': 0.0, 'unattributed_author_packets': 0},
        'quality': {'ambiguous_hops': 0, 'unknown_hops': 0, 'excluded_route_packets': 0,
                    'collapsed_inferred_copies': 0}, 'authors': [], 'repeaters': []}
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='airtime_observations'").fetchone():
        return report
    retained_from = conn.execute('SELECT MIN(observed_at) FROM airtime_observations').fetchone()[0]
    if retained_from is not None:
        report['coverage']['effective_start_time'] = max(start, retained_from)
        report['coverage']['available_seconds'] = max(1.0, now-max(start, retained_from))
    columns = 'observed_at,fingerprint,packet_hash,route,payload_type,frame_bytes,payload_bytes,path_json,bytes_per_hop,airtime_ms,modulation_json,author_key'
    rows = conn.execute(f'SELECT {columns} FROM airtime_observations WHERE observed_at>=? AND observed_at<=? '
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
    authors: dict[str, dict] = {}
    # One group is one evidenced outgoing frame. Direct receptions take precedence
    # over the inferred copy; distinct direct receptions are retained as emissions.
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for at, fingerprint, packet_hash, route, kind, frame_bytes, payload_bytes, path_json, bph, ms, modulation_json, author in rows:
        path = json.loads(path_json)
        author_method = 'advertisement_public_key' if author else 'correlated_decoded_message'
        if not author and packet_hash:
            author_candidates = {key for when, key in links.get(packet_hash, []) if abs(at-when) <= DEDUP_SECONDS}
            author = next(iter(author_candidates)) if len(author_candidates) == 1 else None
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
            if author:
                item = authors.setdefault(author, {**contacts.identity(author), 'rx_airtime_ms': 0.0,
                                                   'received_packets': 0, 'identification_methods': [], 'categories': {
                                                       k: {'airtime_ms': 0.0, 'packets': 0} for k in ('messages','adverts','other')}})
                if author_method not in item['identification_methods']:
                    item['identification_methods'].append(author_method)
                item['rx_airtime_ms'] += ms
                item['received_packets'] += 1
                item['categories'][_category(kind)]['airtime_ms'] += ms
                item['categories'][_category(kind)]['packets'] += 1
            else:
                local['unattributed_author_airtime_ms'] += ms
                local['unattributed_author_packets'] += 1
        if route not in ('FLOOD', 'TRANSPORT_FLOOD') or kind == 'TRACE':
            if at >= start:
                report['quality']['excluded_route_packets'] += 1
            continue
        modulation = json.loads(modulation_json)
        candidates = []
        if author and contacts.relay(author):
            candidates.append((author, (), len(path) == 0, 0))
        for index, prefix in enumerate(path):
            key, status = contacts.resolve(prefix)
            if not key or not contacts.relay(key):
                if at >= start:
                    report['quality']['ambiguous_hops' if status == 'ambiguous' else 'unknown_hops'] += 1
                continue
            candidates.append((key, tuple(path[:index+1]), index == len(path)-1, (index+1)*bph))
        for key, chain, direct, path_bytes in candidates:
            size = 2 + (4 if route.startswith('TRANSPORT_') else 0) + payload_bytes + path_bytes
            hop_ms = ms if direct else lora_airtime_ms(size, **modulation)
            identity = (fingerprint, key, chain)
            previous = groups[identity]
            group = previous[-1] if previous and at-previous[-1]['at'] <= DEDUP_SECONDS else None
            if group is None:
                group = {'at': at, 'inferred_ms': hop_ms, 'direct': []}
                previous.append(group)
            elif not direct and at >= start:
                report['quality']['collapsed_inferred_copies'] += 1
            if direct:
                group['direct'].append((at, hop_ms, True))
    events: dict[str, list] = defaultdict(list)
    for (_, key, _), matches in groups.items():
        for group in matches:
            events[key].extend(group['direct'] or [(group['at'], group['inferred_ms'], False)])
    report['authors'] = sorted(authors.values(), key=lambda item: item['rx_airtime_ms'], reverse=True)
    for key, observations in events.items():
        duty = duty_summary(observations, start, now)
        if duty['packets']:
            report['repeaters'].append({**contacts.identity(key), **duty,
                                        'identification': 'unique_among_known_nodes', 'hardware_tx_counter': False})
    report['repeaters'].sort(key=lambda item: item['airtime_ms'], reverse=True)
    local = report['local']
    if local['rx_packets']:
        local['requested_window_rx_percent'] = round(local['rx_airtime_ms']/1000/seconds*100, 6)
        local['rx_percent'] = round(local['rx_airtime_ms']/1000/report['coverage']['available_seconds']*100, 6)
        local['reference_budget_used_percent'] = round(local['rx_percent']/REFERENCE_PERCENT*100, 4)
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
