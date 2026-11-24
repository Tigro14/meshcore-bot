"""Observed flood scopes for uniquely resolved relay identities."""
import json
import re
import time
from collections import defaultdict


def unscoped_only_relays(conn, days=None, now=None):
    now = time.time() if now is None else now
    prefixes = defaultdict(set)
    relays = set()
    for key, role in conn.execute('SELECT public_key,role FROM complete_contact_tracking'):
        if not isinstance(key, str) or not key:
            continue
        key = key.lower()
        if (role or '').lower() in ('repeater', 'roomserver'):
            relays.add(key)
        for width in (1, 2, 3):
            prefixes[key[:width*2]].add(key)
    query = "SELECT data FROM packet_stream WHERE type='packet' AND timestamp<=?"
    params = [now]
    if days is not None:
        query += ' AND timestamp>=?'
        params.append(now - days * 86400)
    unscoped, scoped = set(), set()
    for raw, in conn.execute(query, params):
        try:
            data = json.loads(raw)
            route = data.get('route_type_name')
            if route not in ('FLOOD', 'TRANSPORT_FLOOD'):
                continue
            routing = data.get('routing_info') or {}
            width = data.get('bytes_per_hop', routing.get('bytes_per_hop'))
            path = data.get('path_hex') or routing.get('path_hex')
            if (type(width) is not int or width not in (1, 2, 3)
                    or not isinstance(path, str) or not re.fullmatch('[0-9a-fA-F]+', path)
                    or len(path) % (width*2)):
                continue
            target = unscoped if route == 'FLOOD' else scoped
            for i in range(0, len(path), width*2):
                candidates = prefixes.get(path[i:i+width*2].lower(), set())
                if len(candidates) == 1:
                    key = next(iter(candidates))
                    if key in relays:
                        target.add(key)
        except (ValueError, TypeError, AttributeError):
            continue
    return unscoped - scoped
