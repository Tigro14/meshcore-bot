"""Channel-monitoring settings and MeshCore channel QR codes for the web viewer.

Two jobs share this module:

- Read and write ``[Channels] monitor_channels`` — the list of channels the bot
  answers on — through the settings store, so the Settings UI can edit it the
  way it edits every other setting (comment-preserving INI write, hot reload).
- Build the ``meshcore://channel/add?name=…&secret=…`` payload the MeshCore
  mobile app reads from an "Add Channel" QR code (see docs.meshcore.io/qr_codes).

The secret of a hashtag channel is publicly derivable — it is the first 16
bytes of ``SHA256("#name")``, the same rule ``ChannelManager.generate_hashtag_key``
uses — so a hashtag QR leaks nothing a name list does not.  A custom (non-hashtag)
channel's secret is its private key: it is only ever handed to the client when the
operator marked the channel publishable in Radio management.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any
from urllib.parse import quote

# What the MeshCore app accepts as a channel name.  Dots are allowed so the
# categorized names already used in [Channels_List] ("sports.sounders") round-trip.
CHANNEL_NAME_RE = re.compile(r'^#?[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$')

MAX_CHANNEL_NAME_LENGTH = 32
MAX_MONITOR_CHANNELS = 40


def hashtag_secret(name: str) -> str:
    """Derive the MeshCore secret of a hashtag channel: SHA256("#name")[:16], hex.

    Mirrors ``ChannelManager.generate_hashtag_key`` (which takes the first 16
    bytes of the same digest) and ``channel_visibility.channel_identity``.
    """
    raw = str(name or "").strip().lower()
    hashtag = raw if raw.startswith('#') else '#' + raw
    return hashlib.sha256(hashtag.encode('utf-8')).hexdigest()[:32]


def normalize_name(name: Any) -> str:
    """Canonical form used in config and comparisons: lowercase, ``#`` for hashtags.

    The bot compares ``message.channel`` against ``monitor_channels`` verbatim,
    so the list has to hold the exact wire name: ``#fr-idf`` for a hashtag
    channel, a bare name for a custom one.
    """
    raw = str(name or "").strip().lower()
    if raw.startswith('#'):
        return '#' + raw[1:].strip()
    return raw


def is_valid_name(name: str) -> bool:
    """Whether a normalized name is safe to write to config.ini and send to the mesh."""
    return bool(name) and len(name) <= MAX_CHANNEL_NAME_LENGTH and bool(CHANNEL_NAME_RE.match(name))


def is_public(name: str) -> bool:
    """Match ``config_validation._channel_name_is_public``: the conventional Public channel."""
    return str(name or "").strip().lstrip('#').lower() == 'public'


def lookup_key(name: Any) -> str:
    """Comparison key for a channel name: lowercase, without the leading ``#``.

    Same rule as ``ChannelManager._normalize_channel_name_for_lookup``.  The
    node's ``channels`` table is not guaranteed to spell a hashtag channel with
    its ``#``, and the monitor list is, so matching has to ignore the prefix.
    """
    return normalize_name(name).removeprefix('#')


def parse_monitor_list(raw: Any) -> list[str]:
    """Parse a ``monitor_channels`` value (or list) into a normalized, ordered, deduped list."""
    parts = raw if isinstance(raw, list) else str(raw or '').split(',')
    names: list[str] = []
    for part in parts:
        name = normalize_name(part)
        if name and name not in names:
            names.append(name)
    return names


def parse_names(values: Any) -> tuple[list[str], list[str]]:
    """Normalize a submitted list, splitting it into accepted names and errors.

    Rejects what would corrupt the file (newlines, commas, ``%``) and what the
    MeshCore app would not accept as a name.  Public is not an error here: the
    caller decides with the override key in hand.
    """
    accepted: list[str] = []
    errors: list[str] = []
    parts = values if isinstance(values, list) else str(values or '').split(',')
    for part in parts:
        raw = str(part).strip()
        if not raw:
            continue
        name = normalize_name(raw)
        if not name or name == '#':
            continue
        if any(c in raw for c in ('\n', '\r', ',', '=')):
            errors.append(f'{raw!r}: un nom de canal ne peut pas contenir de retour à la ligne ni de virgule')
        elif '%' in raw:
            errors.append(f'{raw!r}: un nom de canal ne peut pas contenir "%"')
        elif not is_valid_name(name):
            errors.append(
                f'{raw!r}: nom invalide (1 à {MAX_CHANNEL_NAME_LENGTH} caractères '
                'lettres, chiffres, "-", "_" ou ".", avec un "#" initial pour un canal hashtag)'
            )
        elif name not in accepted:
            accepted.append(name)
    return accepted, errors


def monitor_channel_limit(config: Any) -> int:
    """Upper bound for the monitor list: the radio cannot subscribe to more slots than ``max_channels``."""
    try:
        return max(1, int(config.getint('Bot', 'max_channels', fallback=40)))
    except (TypeError, ValueError, AttributeError):
        return MAX_MONITOR_CHANNELS


def channel_qr(name: str, key_hex: str | None = None, hashtag: bool | None = None) -> dict[str, Any]:
    """Build one QR payload: ``{'qr': {'name', 'secret', 'uri'}}`` or ``{'qr': None}``.

    ``name`` is the wire name (``#fr-idf`` for a hashtag channel).  The MeshCore
    QR carries the name without its ``#`` — the app re-adds it and derives the
    same secret for a hashtag channel.  A custom channel needs its stored key.
    ``hashtag`` overrides the name-based guess when the caller knows the node's
    channel type.
    """
    normalized = normalize_name(name)
    if not normalized:
        return {'qr': None, 'error': 'invalid name'}

    is_hashtag = normalized.startswith('#') if hashtag is None else bool(hashtag)
    if is_hashtag:
        secret = hashtag_secret(normalized)
    else:
        key = str(key_hex or '').strip().lower()
        if len(key) != 32 or not all(c in '0123456789abcdef' for c in key):
            return {'qr': None, 'error': 'no channel key'}
        secret = key

    # The app wants the name without the leading '#'; percent-encode everything
    # outside the unreserved set so a name can never inject a second query param.
    app_name = normalized[1:] if normalized.startswith('#') else normalized
    uri = f"meshcore://channel/add?name={quote(app_name, safe='')}&secret={secret}"
    return {'qr': {'name': app_name, 'secret': secret, 'uri': uri}}


def _is_hashtag_channel(channel: dict[str, Any]) -> bool:
    """Whether a channel's secret is derivable from its name rather than stored.

    ``_monitor_view`` sets an explicit ``hashtag`` flag; raw channel dicts from
    ``_get_channels`` are judged by their name and node-reported channel type.
    """
    if 'hashtag' in channel:
        return bool(channel['hashtag'])
    return normalize_name(channel.get('name')).startswith('#') or channel.get('type') == 'hashtag'


def public_qr_for(channel: dict[str, Any]) -> dict[str, Any] | None:
    """QR payload that may be shown to an anonymous visitor, or None to hide it.

    Hashtag channels: always shareable — their secret is derivable from a name
    already published on the page.  Custom channels: only when the operator
    opted the channel into web publication.  The Public channel is never
    encoded here; its key is not hashtag-derived, and it is not ours to share.
    """
    name = normalize_name(channel.get('name'))
    if not name or is_public(name):
        return None
    is_hashtag = _is_hashtag_channel(channel)
    if is_hashtag:
        return channel_qr(name, hashtag=is_hashtag).get('qr')
    if channel.get('live_messages_visible'):
        return channel_qr(name, channel.get('key_hex'), hashtag=is_hashtag).get('qr')
    return None


def admin_qr_for(channel: dict[str, Any]) -> dict[str, Any] | None:
    """QR payload for an authenticated operator: custom keys included."""
    name = normalize_name(channel.get('name'))
    if not name or is_public(name):
        return None
    is_hashtag = _is_hashtag_channel(channel)
    if is_hashtag:
        return channel_qr(name, hashtag=is_hashtag).get('qr')
    return channel_qr(name, channel.get('key_hex'), hashtag=is_hashtag).get('qr')


class ChannelMonitorMixin:
    """Mixed into BotDataViewer; the ChannelAdminMixin supplies ``_get_channels``."""

    config: Any
    logger: Any
    _get_channels: Any

    def _monitor_view(self) -> dict[str, Any]:
        """Everything the Channels settings page and the public /channels page show.

        The merge follows how the bot reads the setting: ``monitor_channels`` is
        the authoritative list, the radio's channel slots say which of them the
        device can actually hear, and the published-consent table decides which
        QR codes may be shown to a visitor.
        """
        raw = ''
        if self.config and self.config.has_option('Channels', 'monitor_channels'):
            raw = self.config.get('Channels', 'monitor_channels', fallback='') or ''
        monitored = parse_monitor_list(raw)

        channels = [dict(c) for c in self._get_channels()]
        by_name: dict[str, dict[str, Any]] = {}
        for channel in channels:
            key = lookup_key(channel.get('name'))
            if key and key not in by_name:
                by_name[key] = channel

        # Monitored channels first, in config order, then the radio's own slots.
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for name in monitored:
            slot = by_name.get(lookup_key(name))
            entry = dict(slot) if slot else {
                'name': name,
                'channel_name': name,
                'channel_idx': None,
                'index': None,
                'type': 'hashtag' if name.startswith('#') else 'custom',
                'key_hex': None,
                'decode_only': False,
            }
            entry['monitored'] = True
            entry['on_radio'] = slot is not None and slot.get('channel_idx') is not None
            merged.append(entry)
            seen.add(lookup_key(name))
        for channel in channels:
            key = lookup_key(channel.get('name'))
            if key and key not in seen:
                entry = dict(channel)
                entry['monitored'] = False
                entry['on_radio'] = channel.get('channel_idx') is not None
                merged.append(entry)
                seen.add(key)

        for entry in merged:
            # The node's channel_type is authoritative; the '#' prefix is how a
            # hashtag channel spells itself on the wire.
            entry['hashtag'] = entry.get('type') == 'hashtag' or normalize_name(entry.get('name')).startswith('#')

        return {
            'monitor_channels': monitored,
            'limit': monitor_channel_limit(self.config),
            'channels': merged,
        }

    def _missing_hashtag_channels(self, monitored: list[str]) -> list[str]:
        """Monitored hashtag channels the radio is not subscribed to yet.

        Only hashtag channels: adding one needs no secret (the firmware derives
        the key from the name), so it is the safe half of auto-provisioning.
        A custom channel has to be created on the Radio page, where the key is
        supplied deliberately.
        """
        known = {lookup_key(c.get('name')) for c in self._get_channels()}
        return [
            name for name in monitored
            if name.startswith('#') and not is_public(name) and lookup_key(name) not in known
        ]
