"""Evidence-based local offered load, without inventing forwarding obligations.

The analysis project's ECL adds neighbours' TX, not every relay in a path and
not RX plus the same neighbours' TX a second time. Here only evidenced emissions
are available. This is a partial reconstruction, not the project's expected ECL.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from modules.airtime_capture import covered_buckets


def rolling_peak(events: list[tuple[float, float, bool]], start: float, end: float,
                 seconds: int) -> dict:
    """Maximum (t-seconds, t] event sum, including the baseline at start."""
    ordered = sorted((at, ms) for at, ms, _ in events if start-seconds < at <= end)
    if not ordered:
        return {'percent': None, 'ending_at': None}
    peak = sum(ms for at, ms in ordered if at <= start)
    peak_at, rolling, left = start, 0.0, 0
    for right, (at, ms) in enumerate(ordered):
        rolling += ms
        while left <= right and ordered[left][0] <= at-seconds:
            rolling -= ordered[left][1]
            left += 1
        if at >= start and rolling > peak:
            peak, peak_at = rolling, at
    return {'percent': round(peak / (seconds * 1000) * 100, 6), 'ending_at': peak_at}


def reception_history(events: list[tuple[float, float, bool]], start: float, end: float,
                      intervals: list[tuple[float, float]] | None = None, *,
                      frames: list[tuple[float, int]] | None = None, truncated: bool = False) -> dict:
    """One full-frame RX series; fixed buckets and unknown gaps, no TX mixture."""
    duration = end-start
    step = 60 if duration <= 3600 else 900 if duration <= 86400 else 3600
    count = max(1, int((end-start+step-1)//step))
    coverage = covered_buckets(intervals or [], start, end, step)
    totals: dict[int, float] = {}
    packets: dict[int, int] = {}
    sizes: dict[int, list[int]] = {}
    for at, ms, _ in events:
        if start <= at <= end:
            index = min(count-1, int((at-start)//step))
            totals[index] = totals.get(index, 0.0)+ms
            packets[index] = packets.get(index, 0)+1
    for at, size in frames or []:
        if start <= at <= end:
            index = min(count-1, int((at-start)//step))
            sizes.setdefault(index, []).append(size)
    series: list[dict[str, Any]] = []
    for index in range(count):
        left, right = start+index*step, min(end, start+(index+1)*step)
        complete = coverage[index] >= right-left-1e-6
        value = round(totals[index]/step/1000*100, 6) if index in totals else 0.0 if complete else None
        received = packets.get(index, 0)
        rate = round(received/step*60, 6) if received or complete else None
        frame_sizes = sizes.get(index, [])
        mean_size = round(sum(frame_sizes)/received, 3) if received and len(frame_sizes) == received else None
        series.append({'start_time': left, 'end_time': right, 'percent': value,
                       'rx_packets': received if received or complete else None,
                       'rx_packets_per_minute': None if truncated else rate,
                       'rx_mean_frame_bytes': None if truncated else mean_size,
                       'listening_seconds': round(coverage[index], 3),
                       'coverage': 'complete' if complete else 'partial' if coverage[index] else 'unknown'})
    peak = rolling_peak(events, start, end, 300) if totals else {'percent': None, 'ending_at': None}
    if not totals:
        for index, seconds in enumerate(covered_buckets(intervals or [], start, end, 300)):
            if seconds >= 300-1e-6:
                peak = {'percent': 0.0, 'ending_at': start+(index+1)*300}
                break
    return {'rx_bucket_seconds': step,
            'rx_series': series,
            'rx_confirmed_listening_seconds': round(sum(coverage), 3),
            'rx_peak_five_minute_percent': peak['percent'],
            'rx_peak_five_minute_ending_at': peak['ending_at']}


def observer_metadata(conn: sqlite3.Connection) -> dict:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='bot_metadata'").fetchone():
        return {}
    row = conn.execute("SELECT value FROM bot_metadata WHERE key='airtime.observer'").fetchone()
    try:
        data = json.loads(row[0]) if row else {}
        key = data.get('public_key')
        if not isinstance(key, str) or len(key) != 64 or len(bytes.fromhex(key)) != 32:
            return {}
        return {'public_key': key.lower(), 'role': data.get('role', 'unknown')}
    except (ValueError, TypeError, AttributeError):
        return {}


def write_observer_metadata(conn: sqlite3.Connection, self_info: Any) -> None:
    """Publish public radio identity only; never configuration or channel keys."""
    if not isinstance(self_info, dict):
        return
    key = self_info.get('public_key')
    try:
        if not isinstance(key, str) or len(key) != 64 or len(bytes.fromhex(key)) != 32:
            return
    except ValueError:
        return
    raw_role = self_info.get('type')
    role = {1: 'companion', 2: 'repeater', 3: 'roomserver', 4: 'sensor'}.get(raw_role, 'unknown') if isinstance(raw_role, int) else 'unknown'
    data = json.dumps({'public_key': key.lower(), 'role': role}, sort_keys=True)
    conn.execute("INSERT INTO bot_metadata(key,value,updated_at) VALUES ('airtime.observer',?,strftime('%s','now')) "
                 'ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at', (data,))


def local_load(events: dict[str, list[tuple[float, float, bool]]], contacts: Any,
               observer: dict, start: float, end: float, available_start: float,
               rx_airtime_ms: float, retained_from: float | None) -> dict:
    """Sum known local neighbours' evidenced TX, plus evidenced observer origins.

    A neighbour needs a direct RF anchor in the selected period. All other
    repeaters remain remote/unknown even if they dominate the received paths.
    No interpolation across silent hours, extrapolation by presence or 1/FDR
    correction. Neither failed decodes nor unheard transmissions can be restored.
    """
    neighbours = []
    local_events: list[tuple[float, float, bool]] = []
    directly_attributed = 0.0
    for key, node_events in events.items():
        direct = [(at, ms, flag) for at, ms, flag in node_events if start <= at <= end and flag]
        if not direct or key == observer.get('public_key') or not contacts.relay(key):
            continue
        selected = [(at, ms, flag) for at, ms, flag in node_events if start <= at <= end]
        direct_ms = sum(ms for _, ms, _ in direct)
        inferred_ms = sum(ms for _, ms, flag in selected if not flag)
        directly_attributed += direct_ms
        local_events.extend(node_events)
        neighbours.append({**contacts.identity(key), 'direct_receptions': len(direct),
            'direct_airtime_ms': round(direct_ms, 3), 'inferred_airtime_ms': round(inferred_ms, 3),
            'airtime_ms': round(direct_ms+inferred_ms, 3),
            'local_rx_share_percent': round(direct_ms/rx_airtime_ms*100, 6) if rx_airtime_ms else None,
            'first_direct_at': min(at for at, _, _ in direct),
            'last_direct_at': max(at for at, _, _ in direct),
            'fdr': {'status': 'unknown_forwarding_obligations', 'received': len(direct),
                    'expected': None, 'percent': None}})
    neighbours.sort(key=lambda node: node['direct_airtime_ms'], reverse=True)
    own_events = events.get(observer.get('public_key', ''), [])
    local_events.extend(own_events)
    own_ms = sum(ms for at, ms, _ in own_events if start <= at <= end)
    selected_local = [(at, ms, flag) for at, ms, flag in local_events if start <= at <= end]
    total = sum(ms for _, ms, _ in selected_local)
    neighbour_ms = sum(node['airtime_ms'] for node in neighbours)
    seconds = max(1.0, end-available_start)
    # Equal wall-clock buckets; unobserved buckets stay NULL. Always divide by
    # the full bucket duration: a packet near a retained-history boundary must
    # not turn a one-second fragment into an apparent channel saturation.
    step = max(300, int((end-start)/120))
    series: dict[int, float] = {}
    for at, ms, _ in selected_local:
        index = min(int((at-start)/step), max(0, int((end-start-1)/step)))
        series[index] = series.get(index, 0.0)+ms
    buckets = []
    for index in range(max(1, int((end-start+step-1)//step))):
        left, right = start+index*step, min(end, start+(index+1)*step)
        bucket_ms = series.get(index)
        buckets.append({'start_time': left, 'end_time': right,
                        'partial_history': left < available_start or right-left < step,
                        'airtime_ms': round(bucket_ms, 3) if bucket_ms is not None else None,
                        'percent': round(bucket_ms/step/1000*100, 6) if bucket_ms is not None else None})
    return {'status': 'partial_reconstruction' if selected_local else 'no_attributed_local_emission',
            'method': 'evidenced_neighbour_tx_plus_evidenced_observer_origins',
            'expected_ecl_percent': None, 'expected_ecl_status': 'unknown_forwarding_obligations',
            'fdr_status': 'no_independent_expected_transmission_count',
            'estimated_percent': round(total/seconds/1000*100, 6) if selected_local and seconds >= 300 else None,
            'airtime_ms': round(total, 3), 'neighbour_airtime_ms': round(neighbour_ms, 3),
            'observer_evidenced_airtime_ms': round(own_ms, 3),
            'observer_tx_complete': False, 'window_seconds': seconds,
            'directly_attributed_rx_percent': round(directly_attributed/rx_airtime_ms*100, 6) if rx_airtime_ms else None,
            'neighbours': neighbours, 'neighbour_count': len(neighbours),
            'dominant_neighbour': neighbours[0] if neighbours else None,
            'peak_hour': rolling_peak(local_events, start, end, 3600),
            'peak_five_minutes': rolling_peak(local_events, start, end, 300),
            'retained_full_hour': retained_from is not None and retained_from <= end-3600,
            'capture_uptime_known': False, 'missing_transmissions_are_unknown': True,
            'processing_truncated': False,
            'can_exceed_100_percent': True, 'is_duty_cycle': False,
            'bucket_seconds': step, 'buckets': buckets}
