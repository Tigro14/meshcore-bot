"""Observed foreign-to-France flood transitions, without inferred path edges."""
import json
import re
import time
from collections import defaultdict
from datetime import datetime, timezone


def entry_points(conn, days=1, country='', now=None, unscoped_only=False, packet_type="all", target_keys=None, min_packets=1, evidence="all"):
    now = time.time() if now is None else now
    cutoff = now - days * 86400 if days else 0
    contacts = {}
    prefixes = defaultdict(set)
    for row in conn.execute('SELECT public_key,name,country,latitude,longitude FROM complete_contact_tracking WHERE lower(role) IN (\'repeater\',\'roomserver\')'):
        key, name, nation, lat, lon = row
        if not isinstance(key, str) or not key:
            continue
        key = key.lower()
        contacts[key] = dict(public_key=key, name=name or key[:8], country=(nation or '').strip(), latitude=lat, longitude=lon)
        for width in (2, 4, 6):
            prefixes[key[:width]].add(key)
    def is_fr(nation):
        return nation.casefold() in {'france', 'fr', 'français', 'francais'}
    sources = {}
    pairs = {}
    gateways = {}
    countries = set()
    excluded_packets = 0
    excluded_segments = 0
    scanned = 0
    foreign_suffix = 0
    records = []
    retained_since = conn.execute("SELECT MIN(timestamp) FROM packet_stream WHERE type='packet'").fetchone()[0]
    for timestamp, raw in conn.execute("SELECT timestamp,data FROM packet_stream WHERE type='packet' AND timestamp>=? AND timestamp<=? ORDER BY timestamp", (cutoff, now)):
        scanned += 1
        try:
            data = json.loads(raw)
            if data.get('route_type_name') not in ('FLOOD', 'TRANSPORT_FLOOD'):
                continue
            # FLOOD has no transport region header; TRANSPORT_FLOOD does.
            if unscoped_only and data.get('route_type_name') != 'FLOOD':
                continue
            payload = data.get('payload_type_name') or data.get('payload_type')
            if packet_type == 'messages' and payload not in ('TXT_MSG', 'GRP_TXT'):
                continue
            if packet_type == 'adverts' and payload != 'ADVERT':
                continue
            routing = data.get('routing_info') or {}
            packet = data.get('packet_hash') or routing.get('packet_hash')
            path = data.get('path_hex') or routing.get('path_hex')
            width = data.get('bytes_per_hop', routing.get('bytes_per_hop'))
            if (not isinstance(packet, str) or not re.fullmatch(r'[0-9a-fA-F]+', packet) or not packet.strip('0')
                    or type(width) is not int or width not in (1, 2, 3)
                    or not isinstance(path, str) or not re.fullmatch(r'[0-9a-fA-F]+', path)
                    or len(path) % (width * 2)):
                excluded_packets += 1
                continue
            records.append((width, timestamp, packet.lower(), path))
        except (ValueError, TypeError, AttributeError):
            excluded_packets += 1
    for width, timestamp, packet, path in records:
        try:
            hops = [prefixes.get(path[i:i+width*2].lower(), set()) for i in range(0, len(path), width*2)]
            for arrival_hops, (source, dest) in enumerate(zip(hops, hops[1:], strict=False), start=1):
                if not source or len(dest) != 1:
                    excluded_segments += 1
                    continue
                b = next(iter(dest))
                cb = contacts[b]
                if not cb['country']:
                    excluded_segments += 1
                    continue
                if not is_fr(cb['country']):
                    continue
                candidates = [contacts[key] for key in sorted(source)]
                if any(not c['country'] for c in candidates):
                    excluded_segments += 1
                    continue
                french = [is_fr(c['country']) for c in candidates]
                if all(french):
                    continue
                if any(french):
                    excluded_segments += 1
                    continue
                if len(source) == 1:
                    a = next(iter(source))
                    ca = contacts[a]
                else:
                    # Synthetic identity is only a bucket for this prefix, never
                    # an invented repeater or an inferred geographic position.
                    prefix = path[(arrival_hops-1)*width*2:arrival_hops*width*2].lower()
                    a = f'ambiguous:{width}:{prefix}'
                    nations = sorted({c['country'] for c in candidates})
                    ca = dict(public_key=a, name='Relais étranger non identifié',
                              country=nations[0] if len(nations) == 1 else 'Pays étranger indéterminé',
                              latitude=None, longitude=None, ambiguous=True,
                              candidate_count=len(source))
                following = hops[arrival_hops+1:]
                # Reject a crossing followed by a certainly foreign repeater.
                # Unknown or mixed candidates are retained as uncertain evidence.
                if any(h and all(contacts[k]['country'] and not is_fr(contacts[k]['country']) for k in h) for h in following):
                    foreign_suffix += 1
                    continue
                uncertain_suffix = not following or any(not h or any(not is_fr(contacts[k]['country']) for k in h) for h in following)
                inferred = width == 1 or len(source) != 1 or uncertain_suffix
                sources[a] = ca
                countries.add(ca['country'])
                if country and ca['country'].casefold() != country.casefold():
                    continue
                if target_keys is not None and b not in target_keys:
                    continue
                if evidence == 'multibyte' and width == 1:
                    continue
                packet = packet.lower()
                edge = pairs.setdefault((a, b), {'packets': set(), 'last_seen': timestamp, 'short_packets': set(), 'multi_packets': set(), 'inferred_packets': set(), 'reliable_packets': set(), 'arrival_hops': {}})
                edge['packets'].add(packet)
                if inferred:
                    edge['inferred_packets'].add(packet)
                edge['short_packets' if width == 1 else 'multi_packets'].add(packet)
                if not inferred:
                    edge['reliable_packets'].add(packet)
                edge['last_seen'] = max(timestamp, edge['last_seen'])
                edge['arrival_hops'][packet] = min(arrival_hops, edge['arrival_hops'].get(packet, arrival_hops))
        except (ValueError, TypeError, AttributeError):
            excluded_packets += 1
    # Rebuild aggregates from retained links so ranking and histograms share filters.
    pairs = {key: value for key, value in pairs.items() if len(value['packets']) >= min_packets}
    gateways = {}
    for (a, b), edge in pairs.items():
        node = gateways.setdefault(b, {'packets': set(), 'last_seen': edge['last_seen'], 'countries': set(), 'arrival_hops': {}})
        node['packets'].update(edge['packets'])
        node['countries'].add(sources[a]['country'])
        node['last_seen'] = max(node['last_seen'], edge['last_seen'])
        for packet, hops_count in edge['arrival_hops'].items():
            node['arrival_hops'][packet] = min(hops_count, node['arrival_hops'].get(packet, hops_count))
    def date(value):
        return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None
    global_packets = {}
    ranking = []
    for key, value in gateways.items():
        for packet, hops_count in value['arrival_hops'].items():
            global_packets[packet] = min(hops_count, global_packets.get(packet, hops_count))
        histogram = defaultdict(int)
        for hops_count in value['arrival_hops'].values():
            histogram[hops_count] += 1
        ranking.append(dict(contacts[key], count=len(value['packets']),
                            last_seen=date(value['last_seen']), origins=sorted(value['countries']),
                            arrival_hops=[dict(hops=h, count=n) for h, n in sorted(histogram.items())]))
    ranking.sort(key=lambda r: (-r['count'], r['public_key']))
    edges = [dict(source=sources[a], target=contacts[b], count=len(v['packets']), reliable_count=len(v['reliable_packets']), inferred_count=len(v['inferred_packets'] - v['reliable_packets']), short_only_count=len(v['short_packets'] - v['multi_packets']), multi_byte_count=len(v['multi_packets']), last_seen=date(v['last_seen'])) for (a,b),v in pairs.items()]
    edges.sort(key=lambda e: (-e['count'], e['source']['public_key'], e['target']['public_key']))
    global_histogram = defaultdict(int)
    for hops_count in global_packets.values():
        global_histogram[hops_count] += 1
    global_summary = dict(count=len(global_packets),
                          arrival_hops=[dict(hops=h, count=n) for h, n in sorted(global_histogram.items())])
    return dict(global_summary=global_summary, ranking=ranking, edges=edges, countries=sorted(countries), days=days,
                retained_since=date(retained_since), scanned=scanned, excluded_packets=excluded_packets, excluded_segments=excluded_segments, foreign_suffix_segments=foreign_suffix,
                generated_at=date(now), unscoped_only=unscoped_only, packet_type=packet_type)
