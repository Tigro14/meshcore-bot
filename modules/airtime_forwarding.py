"""Historical, one-hop forwarding scenarios for the traffic this observer received.

Training precedes evaluation. A learned radio edge and a positive forwarding
envelope are assumptions, not a configuration readout or a delivery guarantee.
Hypothetical emissions never seed further predictions. Missing returns are not
labelled losses, and neither a scenario duty cycle nor its ratio proves compliance.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time
from collections import Counter, defaultdict
from typing import Any

from modules.channel_occupancy import lora_airtime_ms
from modules.flood_scope import CHANNEL_SCOPE_INHERIT, is_global_marker, normalize_scope_name

TRAIN_SECONDS = 7 * 86400
FRESH_SECONDS = 86400
MIN_TRAIN_SECONDS = 6 * 3600
MIN_SUPPORT = 3
SETTLE_SECONDS = 60
MAX_ROWS = 100000
MAX_OPPORTUNITIES = 100000


def analysis_scope_keys(config: Any, existing: dict | None = None) -> dict[str, bytes]:
    """Use configured region names without changing the command allowlist."""
    keys = {name: key for name, key in (existing or {}).items() if isinstance(key, bytes) and len(key) == 16}
    if config is None:
        return keys
    for section in config.sections():
        for option, value in config.items(section):
            if section == 'Channels' and option.startswith('flood_scope.') and value.strip() == CHANNEL_SCOPE_INHERIT:
                continue
            if option in ('flood_scope', 'startup_flood_scope', 'outgoing_flood_scope_override') or option.startswith('flood_scope.'):
                names = [value]
            elif option == 'flood_scopes' and section in ('Channels', 'Bot'):
                names = value.split(',')
            else:
                continue
            for name in names:
                name = normalize_scope_name(name.strip())
                if not is_global_marker(name):
                    keys[name] = hashlib.sha256(name.encode()).digest()[:16]
    return keys


def create_context_tables(cursor: sqlite3.Cursor) -> None:
    cursor.execute('CREATE TABLE IF NOT EXISTS airtime_packet_context ('
                   'source_id INTEGER PRIMARY KEY,observed_at REAL NOT NULL,'
                   'scope_id TEXT,scope_status TEXT NOT NULL)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_airtime_context_time ON airtime_packet_context(observed_at)')
    cursor.execute('CREATE TABLE IF NOT EXISTS airtime_context_state ('
                   'id INTEGER PRIMARY KEY CHECK(id=1),last_source_id INTEGER NOT NULL,'
                   'updated_at REAL NOT NULL,keyset_digest TEXT NOT NULL)')


def packet_scope(packet: dict, scope_keys: dict[str, bytes]) -> tuple[str | None, str]:
    route = packet.get('route_type_name')
    if route == 'FLOOD':
        return 'global', 'unscoped'
    if route != 'TRANSPORT_FLOOD':
        return None, 'not_flood'
    # A transport code is payload-dependent, not a region identifier. Resolve
    # against known keys, retaining only an opaque digest and no region name/key.
    try:
        codes = packet.get('transport_codes') or {}
        code = codes.get('code1')
        payload_type = packet.get('payload_type')
        payload = bytes.fromhex(packet.get('payload_hex') or '')
        if type(code) is not int or type(payload_type) is not int or not payload or not 0 <= payload_type <= 15:
            return None, 'unknown_scope'
        matches = set()
        for key in scope_keys.values():
            if not isinstance(key, bytes) or len(key) != 16:
                continue
            calculated = int.from_bytes(hmac.new(key, bytes([payload_type])+payload, hashlib.sha256).digest()[:2], 'little')
            calculated = 1 if calculated == 0 else 0xfffe if calculated == 0xffff else calculated
            if calculated == code:
                matches.add(hashlib.sha256(b'airtime-scope-v1:'+key).hexdigest())
        if len(matches) == 1:
            return next(iter(matches)), 'matched_scope'
        return None, 'ambiguous_scope' if matches else 'unknown_scope'
    except (ValueError, TypeError, AttributeError):
        return None, 'unknown_scope'


def ingest_context(conn: sqlite3.Connection, scope_keys: dict[str, bytes], limit: int = 1000) -> int:
    """Separate watermark permits bounded backfill without rewriting RF history."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='airtime_packet_context'").fetchone():
        return 0
    keyset_digest = hashlib.sha256(b''.join(sorted(set(scope_keys.values())))).hexdigest()
    state = conn.execute('SELECT last_source_id,keyset_digest FROM airtime_context_state WHERE id=1').fetchone()
    last_id = state[0] if state and state[1] == keyset_digest else 0
    rows = conn.execute('SELECT id,timestamp,data,type FROM packet_stream WHERE id>? ORDER BY id LIMIT ?',
                        (last_id, limit)).fetchall()
    for source_id, at, raw, kind in rows:
        if kind != 'packet':
            continue
        try:
            packet = json.loads(raw)
            scope_id, status = packet_scope(packet, scope_keys)
            conn.execute('INSERT INTO airtime_packet_context VALUES (?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET '
                         "scope_id=CASE WHEN scope_status='ambiguous_scope' OR excluded.scope_status='ambiguous_scope' "
                         'OR (scope_id IS NOT NULL AND excluded.scope_id IS NOT NULL AND scope_id<>excluded.scope_id) '
                         'THEN NULL ELSE COALESCE(excluded.scope_id,scope_id) END,'
                         "scope_status=CASE WHEN scope_status='ambiguous_scope' OR excluded.scope_status='ambiguous_scope' "
                         'OR (scope_id IS NOT NULL AND excluded.scope_id IS NOT NULL AND scope_id<>excluded.scope_id) '
                         "THEN 'ambiguous_scope' WHEN excluded.scope_id IS NOT NULL THEN excluded.scope_status ELSE scope_status END",
                         (source_id, at, scope_id, status))
        except (ValueError, TypeError, AttributeError):
            continue
    if rows:
        conn.execute('INSERT INTO airtime_context_state VALUES (1,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                     'last_source_id=excluded.last_source_id,updated_at=excluded.updated_at,keyset_digest=excluded.keyset_digest',
                     (rows[-1][0], time.time(), keyset_digest))
    return len(rows)


def _rows(conn: sqlite3.Connection, start: float, end: float) -> tuple[list[dict], bool]:
    context = conn.execute("SELECT 1 FROM sqlite_master WHERE name='airtime_packet_context'").fetchone()
    scope_column = 'c.scope_id' if context else 'NULL'
    join = 'LEFT JOIN airtime_packet_context c ON c.source_id=o.source_id' if context else ''
    rows = conn.execute(f'SELECT o.observed_at,o.fingerprint,o.route,o.payload_type,o.payload_bytes,'
                        f'o.path_json,o.bytes_per_hop,o.modulation_json,o.author_key,{scope_column} '
                        f'FROM airtime_observations o {join} WHERE o.observed_at>=? AND o.observed_at<? '
                        'ORDER BY o.observed_at DESC,o.source_id DESC LIMIT ?', (start, end, MAX_ROWS+1)).fetchall()
    return [dict(zip(('at', 'fp', 'route', 'kind', 'payload_bytes', 'path_json', 'bph', 'profile', 'author', 'scope'), row, strict=True))
            for row in reversed(rows[:MAX_ROWS])], len(rows) > MAX_ROWS


def _decode(row: dict, contacts: Any) -> tuple[list[str], str | None]:
    if row['route'] not in ('FLOOD', 'TRANSPORT_FLOOD') or row['kind'] == 'TRACE':
        return [], 'not_flood'
    if row['bph'] not in (2, 3):
        return [], 'short_identifier'
    path = json.loads(row['path_json'])
    keys = [contacts.resolve(prefix)[0] for prefix in path]
    if any(key is None or not contacts.relay(key) for key in keys):
        return [], 'unresolved_path'
    if len(keys) != len(set(keys)):
        return [], 'looped_path'
    row['scope'] = 'global' if row['route'] == 'FLOOD' else row['scope']
    if row['scope'] is None:
        return [], 'unknown_scope'
    return keys, None


def empty_forwarding_model(start: float, end: float, *, truncated: bool = False) -> dict:
    return {
        'version': 1, 'status': 'insufficient_training', 'basis': 'received_traffic_only',
        'training': {'start_time': start-TRAIN_SECONDS, 'end_time': start,
                     'available_seconds': 0, 'min_support_packets': MIN_SUPPORT,
                     'minimum_history_seconds': MIN_TRAIN_SECONDS, 'freshness_seconds': FRESH_SECONDS,
                     'independent_of_evaluation': True, 'usable_packets': 0, 'supported_edges': 0,
                     'supported_policies': 0},
        'evaluation': {'start_time': start, 'end_time': max(start, end-SETTLE_SECONDS),
                       'settle_seconds': SETTLE_SECONDS, 'distinct_packets': 0, 'modelled_packets': 0,
                       'coverage_percent': None},
        'expected_packets': 0, 'retrieved_packets': 0, 'unretrieved_packets': 0,
        'independent_exposure_packets': 0,
        'additional_airtime_ms': None, 'nodes': [], 'excluded': {}, 'truncated': truncated,
        'local_load': {'scenario_percent': None, 'additional_airtime_ms': None, 'is_duty_cycle': False},
        'is_fdr': False, 'compliance': 'unknown',
        'assumptions': ['historical_links_and_forwarding_envelopes_remain_applicable',
                        'supported_radio_neighbour_receives_upstream_transmission',
                        'at_most_one_extra_forward_per_original_and_relay',
                        'one_hypothetical_hop_only', 'no_unseen_origin_traffic_extrapolation',
                        'unretrieved_is_not_proven_lost', 'observed_hop_depth_is_not_a_configured_limit'],
    }


def build_forwarding_model(conn: sqlite3.Connection, contacts: Any, *, start: float, end: float,
                           authors: dict, evidence_events: dict, local_keys: set[str],
                           local_airtime_ms: float, available_seconds: float,
                           observer_key: str | None = None, truncated: bool = False) -> dict:
    report = empty_forwarding_model(start, end, truncated=truncated)
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='airtime_observations'").fetchone():
        return report
    training, training_truncated = _rows(conn, start-TRAIN_SECONDS, start)
    current, current_truncated = _rows(conn, start, end)
    report['truncated'] = truncated or training_truncated or current_truncated
    if report['truncated']:
        report['status'] = 'processing_limit'
        return report
    if not training:
        return report
    report['training']['available_seconds'] = start-training[0]['at']
    report['training']['requested_start_time'] = start-TRAIN_SECONDS
    report['training']['start_time'] = training[0]['at']
    if report['training']['available_seconds'] < MIN_TRAIN_SECONDS:
        return report
    # The same payload's earlier copy cannot train the model that predicts it.
    evaluation_fps = {row['fp'] for row in current}
    previously_seen = {row['fp'] for row in training}
    edges: dict[tuple, dict] = {}
    policies: dict[tuple, dict] = {}
    training_fps = set()
    for row in training:
        if row['fp'] in evaluation_fps:
            continue
        keys, error = _decode(row, contacts)
        if error:
            continue
        training_fps.add(row['fp'])
        previous = row['author'] if row['author'] and contacts.relay(row['author']) else None
        for depth, key in enumerate(keys):
            policy_id = (key, row['scope'], row['kind'], row['bph'], row['profile'])
            policy = policies.setdefault(policy_id, {'fps': set(), 'depths': defaultdict(set), 'last_at': 0.0})
            policy['fps'].add(row['fp'])
            policy['depths'][depth].add(row['fp'])
            policy['last_at'] = max(policy['last_at'], row['at'])
            if previous and previous != key:
                edge = edges.setdefault((previous, key), {'fps': set(), 'last_at': 0.0})
                edge['fps'].add(row['fp'])
                edge['last_at'] = max(edge['last_at'], row['at'])
            previous = key
    supported: dict[str, list] = defaultdict(list)
    for (source, target), edge in edges.items():
        if len(edge['fps']) >= MIN_SUPPORT:
            supported[source].append((target, edge))
    for policy in policies.values():
        policy['support_by_depth'] = {depth: len(set().union(*(fps for seen_depth, fps in policy['depths'].items()
                                                              if seen_depth >= depth)))
                                      for depth in range(max(policy['depths'])+1)}
    report['training'].update(usable_packets=len(training_fps),
        supported_edges=sum(len(items) for items in supported.values()),
        supported_policies=sum(len(policy['fps']) >= MIN_SUPPORT for policy in policies.values()))
    if not supported:
        return report
    packets: dict[str, list[dict]] = defaultdict(list)
    for row in current:
        packets[row['fp']].append(row)
    exclusions: Counter[str] = Counter()
    matched_packets = set()
    opportunities: dict[tuple, dict] = {}
    seen_forwarders: dict[str, set[str]] = defaultdict(set)
    for fp, copies in packets.items():
        if copies[0]['at'] > end-SETTLE_SECONDS:
            exclusions['awaiting_returns'] += 1
            continue
        report['evaluation']['distinct_packets'] += 1
        if any(row['route'] not in ('FLOOD', 'TRANSPORT_FLOOD') or row['kind'] == 'TRACE' for row in copies):
            exclusions['not_flood'] += 1
            continue
        if fp in previously_seen:
            exclusions['seen_before_evaluation'] += 1
            continue
        author_keys = {authors.get(fp) or row['author'] for row in copies} - {None}
        if len(author_keys) != 1:
            exclusions['unknown_origin'] += 1
            continue
        author = next(iter(author_keys))
        classes = {('global' if row['route'] == 'FLOOD' else row['scope'], row['route'],
                    row['kind'], row['bph'], row['profile']) for row in copies}
        if len(classes) != 1:
            exclusions['inconsistent_packet_context'] += 1
            continue
        for row in copies:
            keys, error = _decode(row, contacts)
            if not error:
                seen_forwarders[fp].update(keys)
        packet_opportunities: dict[tuple, dict] = {}
        packet_errors = set()
        for row in copies:
            keys, error = _decode(row, contacts)
            if error:
                packet_errors.add(error)
                continue
            profile = json.loads(row['profile'])
            base = 2+(4 if row['route'] == 'TRANSPORT_FLOOD' else 0)+row['payload_bytes']
            times = [lora_airtime_ms(base+depth*row['bph'], **profile) for depth in range(len(keys)+1)]
            stages = ([(author, 0)] if contacts.relay(author) else []) + [(key, depth+1) for depth, key in enumerate(keys)]
            for source, input_hops in stages:
                upstream_at = row['at']-sum(times[input_hops+1:])/1000
                for target, edge in supported.get(source, []):
                    if target == author or target == observer_key or target in keys[:input_hops]:
                        continue
                    target_policy = policies.get((target, row['scope'], row['kind'], row['bph'], row['profile']))
                    if not target_policy or len(target_policy['fps']) < MIN_SUPPORT:
                        packet_errors.add('unsupported_forwarding_class')
                        continue
                    if upstream_at-min(edge['last_at'], target_policy['last_at']) > FRESH_SECONDS:
                        packet_errors.add('stale_training')
                        continue
                    depth_support = target_policy['support_by_depth'].get(input_hops, 0)
                    if depth_support < MIN_SUPPORT or (input_hops+1)*row['bph'] > 64:
                        packet_errors.add('unsupported_hop_depth')
                        continue
                    frame_ms = lora_airtime_ms(base+(input_hops+1)*row['bph'], **profile)
                    at = upstream_at+frame_ms/1000
                    if not start <= at <= end-SETTLE_SECONDS:
                        continue
                    identity = (fp, target)
                    candidate = {'public_key': target, 'at': at, 'airtime_ms': frame_ms,
                                 'input_hops': input_hops, 'edge_support': len(edge['fps']),
                                 'policy_support': depth_support, 'source': source,
                                 'independent_exposure': target not in keys}
                    previous = packet_opportunities.get(identity)
                    if previous is not None:
                        independent = previous['independent_exposure'] or candidate['independent_exposure']
                        previous['independent_exposure'] = candidate['independent_exposure'] = independent
                    if previous is None or (input_hops, at) < (previous['input_hops'], previous['at']):
                        packet_opportunities[identity] = candidate
        if packet_opportunities:
            matched_packets.add(fp)
            opportunities.update(packet_opportunities)
        else:
            exclusions.update(packet_errors or {'no_supported_radio_edge'})
        if len(opportunities) > MAX_OPPORTUNITIES:
            report['status'] = 'processing_limit'
            report['truncated'] = True
            return report
    nodes: dict[str, dict] = {}
    additional_events: dict[str, list] = defaultdict(list)
    for (fp, key), opportunity in opportunities.items():
        node = nodes.setdefault(key, {**contacts.identity(key), 'expected_packets': 0,
            'retrieved_packets': 0, 'unretrieved_packets': 0, 'additional_airtime_ms': 0.0,
            'independent_exposure_packets': 0,
            'last_hour_expected_packets': 0,
            'local_neighbour': key in local_keys, 'edge_support_min': opportunity['edge_support'],
            'policy_support_min': opportunity['policy_support']})
        node['expected_packets'] += 1
        node['independent_exposure_packets'] += int(opportunity['independent_exposure'])
        node['last_hour_expected_packets'] += int(end-3600 < opportunity['at'] <= end)
        node['edge_support_min'] = min(node['edge_support_min'], opportunity['edge_support'])
        node['policy_support_min'] = min(node['policy_support_min'], opportunity['policy_support'])
        if key in seen_forwarders[fp]:
            node['retrieved_packets'] += 1
        else:
            node['unretrieved_packets'] += 1
            node['additional_airtime_ms'] += opportunity['airtime_ms']
            additional_events[key].append((opportunity['at'], opportunity['airtime_ms']))
    local_extra = 0.0
    for key, node in nodes.items():
        observed = [(at, ms) for at, ms, _ in evidence_events.get(key, []) if start <= at <= end]
        last_observed = [(at, ms) for at, ms in observed if end-3600 < at]
        last_extra = [(at, ms) for at, ms in additional_events[key] if end-3600 < at <= end]
        node.update(additional_airtime_ms=round(node['additional_airtime_ms'], 3),
            reconstructed_airtime_ms=round(sum(ms for _, ms in observed), 3),
            reconstructed_hour_percent=round(sum(ms for _, ms in last_observed)/3600000*100, 6) if last_observed else None,
            scenario_hour_percent=round((sum(ms for _, ms in last_observed)+sum(ms for _, ms in last_extra))/3600000*100, 6)
                if node['last_hour_expected_packets'] and (last_observed or last_extra) else None,
            scenario_hour_status='supported_opportunities' if node['last_hour_expected_packets'] else 'no_supported_hourly_opportunity',
            is_fdr=False, compliance='unknown')
        if key in local_keys:
            local_extra += node['additional_airtime_ms']
    report.update(status='partial_scenario' if nodes else 'no_supported_opportunity', nodes=sorted(nodes.values(),
        key=lambda node: (node['unretrieved_packets'], node['expected_packets']), reverse=True), excluded=dict(exclusions),
        expected_packets=sum(node['expected_packets'] for node in nodes.values()),
        retrieved_packets=sum(node['retrieved_packets'] for node in nodes.values()),
        unretrieved_packets=sum(node['unretrieved_packets'] for node in nodes.values()),
        independent_exposure_packets=sum(node['independent_exposure_packets'] for node in nodes.values()),
        additional_airtime_ms=round(sum(node['additional_airtime_ms'] for node in nodes.values()), 3) if nodes else None)
    report['evaluation'].update(modelled_packets=len(matched_packets),
        coverage_percent=round(len(matched_packets)/report['evaluation']['distinct_packets']*100, 6)
            if report['evaluation']['distinct_packets'] else None)
    report['local_load'].update(additional_airtime_ms=round(local_extra, 3) if nodes else None,
        scenario_percent=round((local_airtime_ms+local_extra)/max(1, available_seconds)/1000*100, 6)
            if any(node['local_neighbour'] for node in nodes.values()) and available_seconds >= 300 else None)
    return report
