"""Rank unambiguous relays by distinct observed packet hashes, never adverts sent."""
from collections import defaultdict
import re
import json
import time


def relay_usage(conn, hours=24, limit=3, budget=220):
    contacts = conn.execute("SELECT public_key,name FROM complete_contact_tracking WHERE lower(role) IN ('repeater','roomserver')").fetchall()
    prefixes = defaultdict(set)
    names = {}
    for key, name in contacts:
        if not key:
            continue
        key = key.lower()
        names[key] = name or key[:8]
        for width in (2, 4, 6):
            prefixes[key[:width]].add(key)
    packets = defaultdict(set)
    skipped = False
    now = time.time()
    rows = conn.execute("SELECT data FROM packet_stream WHERE type='packet' AND timestamp>=? AND timestamp<=?", (now-hours*3600, now))
    for (raw,) in rows:
        try:
            data = json.loads(raw)
            routing = data.get('routing_info') or {}
            # Direct routes can encode a planned path, not a traversed path.
            if data.get('route_type_name') not in ('FLOOD', 'TRANSPORT_FLOOD'):
                continue
            packet = data.get('packet_hash') or routing.get('packet_hash')
            path = data.get('path_hex') or routing.get('path_hex')
            width = data.get('bytes_per_hop', routing.get('bytes_per_hop'))
        except (ValueError, TypeError, AttributeError):
            skipped = True
            continue
        if not isinstance(packet, str) or not packet.strip('0') or type(width) is not int or width not in (1, 2, 3) or not isinstance(path, str):
            skipped = True
            continue
        size = width * 2
        if not path or len(path) % size or not re.fullmatch('[0-9a-fA-F]+', path):
            skipped = True
            continue
        for prefix in set(path[i:i+size].lower() for i in range(0, len(path), size)):
            keys = prefixes.get(prefix, set())
            if len(keys) != 1:
                skipped = True
                continue
            packets[next(iter(keys))].add(str(packet).lower())
    if not packets:
        return f'Aucun relais identifiable avec certitude dans les chemins observés sur {hours} h.'
    lines = [f'Top RPT / {hours} h — paquets reçus :']
    for key in sorted(packets, key=lambda k: (-len(packets[k]), k))[:limit]:
        name = ' '.join(str(names[key]).split()).encode()[:30].decode(errors='ignore')
        line = f'{name} : {len(packets[key])}'
        if len('\n'.join(lines+[line]).encode()) > budget:
            break
        lines.append(line)
    if len(lines) == 1:
        return 'Budget insuffisant pour afficher le classement.'
    if skipped and len(('\n'.join(lines)+'\nAmbigus/inconnus exclus.').encode()) <= budget:
        lines.append('Ambigus/inconnus exclus.')
    return '\n'.join(lines)
