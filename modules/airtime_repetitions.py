"""Received-copy accounting and independent RF roles, using retained metadata only.

The baseline is the first local reception of each payload fingerprint inside the
selected period. Further receptions are additional copies, including useful flood
retransmissions. Their airtime is never inferred, extrapolated, or called waste.
Author cost and receiver-side role ledgers describe different facts and are not added.
"""
from __future__ import annotations

import bisect
import json
import sqlite3
from typing import Any

from modules.airtime_capture import covered_buckets, listening_intervals

def _counts(packets: int, distinct: int, total: float, first: float, *, truncated: bool) -> dict:
    extra = max(0.0, total-first)
    return {
        'status': 'truncated' if truncated else 'available' if packets else 'no_receptions',
        'received_packets': packets, 'distinct_packets': distinct,
        'additional_received_copies': packets-distinct,
        'rx_airtime_ms': total, 'first_received_airtime_ms': first,
        'additional_received_airtime_ms': None if truncated else extra,
        'additional_received_airtime_percent': round(extra/total*100, 6) if total and not truncated else None,
        'repetition_factor': round(packets/distinct, 6) if distinct and not truncated else None,
    }


def received_insights(rows: list, contacts: Any, authors: dict[str, str | None], *,
                      start: float, end: float, bins: list[dict], truncated: bool = False) -> dict:
    """Consume chronologically ordered airtime_observations rows (without source_id).

    A full payload fingerprint, not the short packet hash, establishes identity.
    Unknown or ambiguous relay prefixes remain labelled bytes. Only FLOOD paths
    establish RF emitters/entry relays; direct-route instructions and TRACE do not.
    """
    seen: set[str] = set()
    first_ms = total_ms = 0.0
    count = 0
    starts = [point['start_time'] for point in bins]
    buckets: dict[int, dict] = {}
    emitter_groups: dict[str, dict] = {}
    entry_groups: dict[str, dict] = {}
    unknown: dict[str, Any] = {'name': 'Émetteur non identifiable', 'public_key': None, 'prefix': None,
               'identification': 'unknown', 'received_packets': 0, 'rx_airtime_ms': 0.0,
               'categories': {'messages': 0, 'adverts': 0, 'other': 0}}

    def identity(prefix: str) -> tuple[str, dict]:
        key, reason = contacts.resolve(prefix)
        if key and contacts.relay(key):
            return 'key:'+key, {**contacts.identity(key), 'prefix': prefix, 'identification': reason}
        return 'prefix:'+prefix, {'name': prefix, 'public_key': None, 'prefix': prefix,
                                  'identification': reason if not key else 'unconfirmed_relay_role'}

    def add(groups: dict, label: str, node: dict, ms: float, category: str) -> None:
        item = groups.setdefault(label, {**node, 'received_packets': 0, 'rx_airtime_ms': 0.0,
                                        'categories': {'messages': 0, 'adverts': 0, 'other': 0}})
        item['received_packets'] += 1
        item['rx_airtime_ms'] += ms
        item['categories'][category] += 1

    for row in rows:
        at, fingerprint, _, route, kind, _, _, path_json, _, ms, _, _ = row
        if not start <= at <= end:
            continue
        extra = fingerprint in seen
        seen.add(fingerprint)
        count += 1
        total_ms += ms
        if not extra:
            first_ms += ms
        index = bisect.bisect_right(starts, at)-1
        if index >= 0 and at <= bins[index]['end_time']:
            bucket = buckets.setdefault(index, {'packets': 0, 'fingerprints': set(), 'ms': 0.0, 'first_ms': 0.0})
            bucket['packets'] += 1
            bucket['fingerprints'].add(fingerprint)
            bucket['ms'] += ms
            if not extra:
                bucket['first_ms'] += ms
        category = 'adverts' if kind == 'ADVERT' else 'messages' if kind in ('GRP_TXT', 'TXT_MSG') else 'other'
        path = json.loads(path_json)
        flood = route in ('FLOOD', 'TRANSPORT_FLOOD') and kind != 'TRACE'
        author = authors.get(fingerprint)
        if flood and path:
            label, node = identity(path[-1])
            add(emitter_groups, label, node, ms, category)
            label, node = identity(path[0])
            add(entry_groups, label, node, ms, category)
        elif flood and not path and author:
            add(emitter_groups, 'key:'+author, {**contacts.identity(author), 'prefix': None,
                                          'identification': 'direct_identified_author'}, ms, category)
        else:
            unknown['received_packets'] += 1
            unknown['rx_airtime_ms'] += ms
            unknown['categories'][category] += 1

    series = []
    for index, point in enumerate(bins):
        item = buckets.get(index)
        counts = _counts(item['packets'], len(item['fingerprints']), item['ms'], item['first_ms'],
                         truncated=truncated) if item else None
        series.append({'start_time': point['start_time'], 'end_time': point['end_time'],
                       'distinct_packets': counts['distinct_packets'] if counts else 0 if point['coverage'] == 'complete' else None,
                       'additional_received_airtime_percent': counts['additional_received_airtime_percent'] if counts else None})
    return {
        **_counts(count, len(seen), total_ms, first_ms, truncated=truncated),
        'scope': 'local_receptions', 'identity_basis': 'payload_fingerprint',
        'baseline': 'first_local_reception_in_selected_period',
        'series': series,
        'roles': {
            'basis': 'received_copies_only', 'additive_across_roles': False,
            'status': 'truncated' if truncated else 'observed',
            'emitters': sorted(emitter_groups.values(), key=lambda node: node['rx_airtime_ms'], reverse=True),
            'unknown_emitter': unknown,
            'entry_relays': sorted(entry_groups.values(), key=lambda node: node['rx_airtime_ms'], reverse=True),
        },
    }


def compare_receptions(current: dict, previous: dict, *, complete: bool) -> dict:
    """Compare like-sized periods only when their local listening is continuous."""
    usable = complete and current['status'] == previous['status'] == 'available'
    return {
        'status': 'comparable' if usable else 'incomplete_observation',
        'previous': {key: previous[key] for key in (
            'distinct_packets', 'received_packets', 'rx_airtime_ms', 'repetition_factor',
            'additional_received_airtime_percent')},
        'additional_airtime_change_points': round(current['additional_received_airtime_percent']
            - previous['additional_received_airtime_percent'], 6) if usable else None,
        'distinct_packets_change_percent': round((current['distinct_packets']/previous['distinct_packets']-1)*100, 6)
            if usable and previous['distinct_packets'] else None,
        'received_airtime_change_percent': round((current['rx_airtime_ms']/previous['rx_airtime_ms']-1)*100, 6)
            if usable and previous['rx_airtime_ms'] else None,
    }


def closed_period_comparison(conn: sqlite3.Connection, *, now: float, seconds: int,
                             retained_from: float | None, limit: int) -> dict:
    """Close both equal windows at the latest fresh confirmed listening timestamp.

    The writer publishes healthy intervals shortly behind the request time. Waiting
    for confirmation of the last fractional second would make every live comparison
    incomplete. Close at that confirmation instead; never fill an internal gap.
    """
    intervals = listening_intervals(conn, now-2*seconds-75, now)
    anchor = min(now, max((right for _, right in intervals), default=now))
    start = anchor-2*seconds
    if retained_from is None or retained_from > start:
        return {'status': 'insufficient_history'}
    samples = conn.execute('SELECT observed_at,fingerprint,airtime_ms FROM airtime_observations '
                           'WHERE observed_at>=? AND observed_at<? ORDER BY observed_at,source_id LIMIT ?',
                           (start, anchor, 2*limit+1)).fetchall()
    metrics = []
    for left, right in ((start, anchor-seconds), (anchor-seconds, anchor)):
        selected = [(fp, ms) for at, fp, ms in samples if left <= at < right]
        seen: set[str] = set()
        total = first = 0.0
        for fp, ms in selected:
            total += ms
            if fp not in seen:
                first += ms
            seen.add(fp)
        metrics.append(_counts(len(selected), len(seen), total, first,
                               truncated=len(samples) > 2*limit or len(selected) > limit))
    coverage = covered_buckets(intervals, start, anchor, seconds)
    result = compare_receptions(metrics[1], metrics[0], complete=(
        bool(intervals) and now-anchor <= 75 and all(value >= seconds-1e-6 for value in coverage)))
    result.update({'start_time': anchor-seconds, 'end_time': anchor,
                   'previous_start_time': start, 'window_seconds': seconds,
                   'basis': 'closed_confirmed_local_listening_windows',
                   'current': {key: metrics[1][key] for key in (
                       'distinct_packets', 'received_packets', 'rx_airtime_ms', 'repetition_factor',
                       'additional_received_airtime_percent')}})
    return result
