"""Resolve a proximity origin from sender GPS, then the first incoming RF hop."""
import math
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Origin:
    latitude: float
    longitude: float
    public_key: str
    label: str = ''


def coordinates(row):
    try:
        lat, lon = float(row[2]), float(row[3])
        if math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180 and (lat, lon) != (0, 0):
            return lat, lon
    except (TypeError, ValueError):
        pass
    return None


def first_hop(message):
    """Incoming path order is sender -> relays -> bot. Never use an outgoing path."""
    routing = message.routing_info
    if isinstance(routing, dict):
        if routing.get('path_length') == 0:
            return None
        nodes = routing.get('path_nodes')
        width = routing.get('bytes_per_hop')
        if width is not None and (type(width) is not int or width not in (1, 2, 3)):
            return None
        if nodes:
            if not isinstance(nodes, (list, tuple)) or not all(isinstance(n, str) for n in nodes):
                return None
            size = 2 * width if type(width) is int and width in (1, 2, 3) else len(nodes[0])
            if size not in (2, 4, 6) or not all(re.fullmatch(r'[0-9a-fA-F]{%d}' % size, n) for n in nodes):
                return None
            return nodes[0].lower()
        raw = routing.get('path_hex')
        if raw:
            if type(width) is not int or width not in (1, 2, 3) or not isinstance(raw, str):
                return None
            if len(raw) % (2 * width) or not re.fullmatch('[0-9a-fA-F]+', raw):
                return None
            return raw[:2 * width].lower()
    # Legacy strings already have one comma-delimited token per hop. Do not
    # split a contiguous hex string without encoding metadata.
    raw = (message.path or '').split(' via ROUTE_TYPE_')[0].split('(')[0].strip()
    nodes = [n.strip() for n in raw.split(',')]
    if not nodes or len(nodes[0]) not in (2, 4, 6):
        return None
    if not all(re.fullmatch(r'[0-9a-fA-F]{%d}' % len(nodes[0]), n) for n in nodes):
        return None
    return nodes[0].lower()


def resolve_origin(conn, message):
    fields = 'public_key, name, latitude, longitude'
    key = (message.sender_pubkey or '').strip()
    # A provided public key is authoritative: a duplicate name must not override it.
    if key:
        rows = conn.execute(f'SELECT {fields} FROM complete_contact_tracking WHERE public_key = ? COLLATE NOCASE', (key,)).fetchall()
    else:
        rows = conn.execute(f'SELECT {fields} FROM complete_contact_tracking WHERE name = ? OR public_key = ? COLLATE NOCASE', (message.sender_id or '', message.sender_id or '')).fetchall()
    if len(rows) == 1 and coordinates(rows[0]):
        return Origin(*coordinates(rows[0]), rows[0][0]), None
    prefix = first_hop(message)
    if prefix is None:
        return None, 'GPS absent et aucun premier relais exploitable dans ce message.'
    # Count all matching repeaters BEFORE checking GPS. Missing GPS must not
    # make a prefix collision look unique.
    rows = conn.execute(f"SELECT {fields} FROM complete_contact_tracking WHERE lower(public_key) LIKE ? AND lower(role) IN ('repeater','roomserver')", (prefix + '%',)).fetchall()
    if len(rows) > 1:
        return None, 'GPS absent : le premier saut correspond à plusieurs relais. Localisation indéterminée.'
    if not rows or not coordinates(rows[0]):
        return None, 'GPS absent : premier relais inconnu ou sans coordonnées.'
    row = rows[0]
    name = re.sub(r'\s+', ' ', str(row[1] or prefix)).strip()
    # Stable reference label distinguishes this approximation from sender GPS.
    name = name.encode('utf-8')[:32].decode('utf-8', errors='ignore')
    return Origin(*coordinates(row), row[0], f'Repère : 1er relais {name} (approx.).'), None
