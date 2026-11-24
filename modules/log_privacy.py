"""Keep message bodies in the processing layer, not in web log output."""
import json
import math
import re

_ROUTES = {'UNKNOWN', 'TRANSPORT_FLOOD', 'TRANSPORT_DIRECT', 'FLOOD', 'DIRECT',
           'TRANSPORT_SCOPED_FLOOD', 'TRANSPORT_TRANSPORT'}
_ANSI = re.compile(r'\x1b\[[0-9;]*m')
_PREFIX = re.compile(r'^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+Z?)?) - ([A-Za-z0-9_.-]+) - (DEBUG|INFO|WARNING|ERROR|CRITICAL) - (.*)$')


def _number(value, *, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return str(int(value)) if integer else f'{value:g}'


def message_metadata(message):
    """Do not mutate the message: the bot/statistics still need its full body."""
    private = getattr(message, 'is_dm', False) is True
    routing = getattr(message, 'routing_info', None)
    route = routing.get('route_type', 'UNKNOWN') if isinstance(routing, dict) else 'UNKNOWN'
    route = route if isinstance(route, str) and route in _ROUTES else 'UNKNOWN'
    result = f"📡 {'PRIVATE' if private else 'CHANNEL'} MESSAGE: {route} | Type: {'TXT_MSG' if private else 'GRP_TXT'}"
    for attr, label, integer, unit in [('hops','Hops',True,''),('snr','SNR',False,' dB'),('rssi','RSSI',False,' dBm')]:
        value = _number(getattr(message, attr, None), integer=integer)
        if value is not None:
            result += f' | {label}: {value}{unit}'
    body = getattr(message, 'original_content', None) or getattr(message, 'content', '')
    if isinstance(body, str):
        result += f' | Bytes: {len(body.encode("utf-8"))}'
    return result


_MASK = '[contenu masqué]'
# Match field names, not words inside technical descriptions (e.g. payload size).
# Packet bytes/hex, paths, hashes, public keys and RF measurements stay visible.
_SENSITIVE_KEYS = (
    r'text|content|original_content|message_content|message|messages|prompt|'
    r'question|answer|query|arguments|server_decrypted\w*|decrypted_text|'
    r'decrypted_message|decrypted_payload|private_key|channel_key|channel_key_hex|key_hex|secret_key|secret|psk|'
    r'api_key|password|access_token|token|jwt'
)
_FIELDS = re.compile(
    r"(?P<label>['\"](?:" + _SENSITIVE_KEYS + r")['\"]\s*:\s*|"
    r"\b(?:" + _SENSITIVE_KEYS + r")\s*=\s*)", re.IGNORECASE
)
# Old log files and optional plugins can contain free-text conversation dumps.
# Keep the useful label, but never replay that dump to the browser.
_BODY_DUMP = re.compile(
    r"(?P<label>\b(?:Processing message|Extracted (?:message )?content|"
    r"(?:Path|Repeater) command executed with content|"
    r"LLM (?:prompt|raw response|response|retry response|cleaned response)(?:[^:]*?)|"
    r"Mesh service (?:generated SQL|repaired SQL|SQL results)|"
    r"Formatted mesh info text|Shlink response|"
    r"Processing scheduled message|Invalid scheduled message format|"
    r"(?:Queued message for|Posted to) (?:Discord|Telegram)[^:]*|"
    r"Announcement sent to [^:]*|Rain nowcast[^:]*sent to [^:]*|"
    r"Lightning alert sent|World Cup live update|JWT Payload|"
    r"(?:message content|message_content|text|content|prompt|question|answer)"
    r")\s*:\s*)", re.IGNORECASE
)
_RECEIVED_DUMP = re.compile(
    r"^(?:Received (?:channel |private |DM )?message\b|"
    r"Sending channel message to\b).*:", re.IGNORECASE
)


def _value_end(body, start):
    """Find a Python/JSON field value without splitting escaped or nested text.

    Unknown/unquoted values consume the rest of the line: old debug reprs are
    sometimes truncated, and retaining their remainder could leak message text.
    """
    if start >= len(body):
        return start
    opening = body[start]
    if opening not in "\"'{[(":
        return len(body)
    quote = None
    escaped = False
    stack = []
    for pos in range(start, len(body)):
        char = body[pos]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
                if not stack:
                    return pos + 1
        elif char in "\"'":
            quote = char
        elif char in '{[(':
            stack.append({'{': '}', '[': ']', '(': ')'}[char])
        elif stack and char == stack[-1]:
            stack.pop()
            if not stack:
                return pos + 1
    return len(body)


def _redact_fields(body):
    parts = []
    cursor = 0
    while match := _FIELDS.search(body, cursor):
        parts.extend((body[cursor:match.end()], repr(_MASK)))
        cursor = _value_end(body, match.end())
    parts.append(body[cursor:])
    return ''.join(parts)


def _web_body(body):
    # Keep current metadata and the legacy zero-hop radio label readable.
    if body.startswith('📡 DIRECT MESSAGE:') and re.search(r'\| Type: GRP_(?:TXT|DATA)\b', body):
        body = body.replace('📡 DIRECT MESSAGE:', '📡 CHANNEL MESSAGE:', 1)
    if _RECEIVED_DUMP.match(body):
        return body.split(':', 1)[0] + ': ' + _MASK
    if re.fullmatch(r'Catalog tokens prompt=\d+ cached=\d+ completion=\d+', body):
        return body
    dump = _BODY_DUMP.search(body)
    if dump:
        return _redact_fields(body[:dump.end()]) + _MASK
    return _redact_fields(body)


def web_log_line(line):
    """Keep technical logs readable; remove conversation fields and legacy dumps.

    The same targeted redaction applies to live lines and history. Unprefixed
    continuations are withheld because they may belong to a multiline message.
    This is not an allowlist of technical events: new diagnostic lines survive.
    """
    line = _ANSI.sub('', str(line)).rstrip('\r\n')
    if line.startswith('{'):
        try:
            item = json.loads(line)
            line = f"{item['timestamp']} - {item['logger']} - {item['level']} - {item['message']}"
        except (ValueError, KeyError, TypeError):
            return None
    match = _PREFIX.fullmatch(line)
    if not match:
        return None
    return f'{match[1]} - {match[2]} - {match[3]} - ' + _web_body(match[4])
