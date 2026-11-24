"""Count only the received message's observed hops, never a stored return path."""
from ..packet_decode import decode_path_len_byte


def _count(value):
    return value if type(value) is int and 0 <= value <= 63 else None


def observed_hops(message):
    raw = message.received_path_len
    if raw == 255:
        return None  # Companion explicitly marks a non-flood receive frame.
    if type(raw) is int and 0 <= raw <= 255 and raw != 255:
        decoded = decode_path_len_byte(raw)
        if decoded is not None:
            byte_length, width = decoded
            return byte_length // width
    routing = message.routing_info or {}
    route = routing.get('route_type', '')
    route = getattr(route, 'name', route)
    if type(route) is int:
        route = {0:'TRANSPORT_FLOOD',1:'FLOOD',2:'DIRECT',3:'TRANSPORT_DIRECT'}.get(route, '')
    if route in {'FLOOD','TRANSPORT_FLOOD'}:
        count = _count(routing.get('path_length'))
        if count is not None:
            return count
    # A DIRECT header lists remaining relays, consumed along the route. Its
    # zero at arrival does not establish that the sender was one RF hop away.
    if route in {'DIRECT','TRANSPORT_DIRECT'} or message.is_dm:
        return None
    if raw is None:
        return _count(message.hops)
    return None


def hops_reply(message):
    count = observed_hops(message)
    if count is None:
        if message.is_dm:
            return 'Je reçois ton message, mais ce DM ne fournit pas le nombre de relais traversés.'
        return 'Le nombre de sauts de ce message n’est pas disponible.'
    if count == 0:
        return 'Ton message m’est parvenu directement, sans relais (0 saut).'
    return f'Ton message m’est parvenu après {count} saut{"s" if count > 1 else ""}.'
