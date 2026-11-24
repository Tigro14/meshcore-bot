"""Exclude identified repeater origins from LLM use in DMs and channels."""

import json

from ..dm_contacts import DmContactResolver


def sender_may_use_llm(bot, message) -> bool:
    """Use the origin's advertised type, never its name or a repeater in the path.

    Unknown types and ambiguous identities are allowed. The historical `role`
    column is deliberately ignored: older rows may contain name-based guesses.
    This lookup does not query or modify the radio.
    """
    identifier = message.sender_pubkey or message.sender_id
    try:
        resolver = DmContactResolver(bot)
        key, row, _ = resolver.identify(identifier)
        if not key:
            return True
        records = resolver._radio_contacts() + resolver._radio_contacts(pending=True)
        native_types = {
            record['type'] for record in records
            if resolver._key(record) == key and type(record.get('type')) is int
        }
        if native_types:
            return 2 not in native_types
        raw = row.get('raw_advert_data') if isinstance(row, dict) else None
        if isinstance(raw, str):
            raw = json.loads(raw)
        if not isinstance(raw, dict):
            return True
        if type(raw.get('type')) is int:
            return raw['type'] != 2
        return str(raw.get('mode', '')).casefold() != 'repeater'
    except Exception:
        # A failed lookup does not establish that the origin is a repeater.
        return True
