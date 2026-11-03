"""Explicit per-channel consent to publish decrypted messages on the web."""

import hashlib


def normalized_name(name):
    return str(name or "").strip().lstrip("#").casefold()


def channel_identity(name, key_hex=None):
    """Bind consent to both the channel name and key, never a reusable slot."""
    raw_name = str(name or "").strip().lower()
    name = normalized_name(name)
    if not name:
        return None
    if not key_hex:
        hashtag = raw_name if raw_name.startswith("#") else "#" + raw_name
        key_hex = hashlib.sha256(hashtag.encode()).hexdigest()[:32]
    try:
        key = bytes.fromhex(key_hex)
    except (ValueError, TypeError):
        return None
    if len(key) != 16:
        return None
    return hashlib.sha256(name.encode() + b"\0" + key).hexdigest()


def permitted_ids(conn):
    return {
        row[0]
        for row in conn.execute("SELECT channel_identity FROM channel_web_visibility WHERE live_messages_visible = 1")
    }


def set_visibility(conn, identity, visible):
    if not identity or type(visible) is not bool:
        raise ValueError("Invalid channel visibility")
    conn.execute(
        """INSERT INTO channel_web_visibility(channel_identity, live_messages_visible)
                    VALUES (?, ?) ON CONFLICT(channel_identity) DO UPDATE
                    SET live_messages_visible=excluded.live_messages_visible""",
        (identity, int(visible)),
    )


def message_identity(conn, name):
    """Ambiguous or unknown names are not safe to publish."""
    candidates = {
        channel_identity(row[0], row[1])
        for row in conn.execute("SELECT channel_name, channel_key_hex FROM channels")
        if normalized_name(row[0]) == normalized_name(name)
    }
    candidates.discard(None)
    return next(iter(candidates)) if len(candidates) == 1 else None


def can_publish(conn, data, *, command=False):
    if not isinstance(data, dict):
        return False
    if command and (data.get("is_dm") is True or data.get("channel") in (None, "DM", "Direct Message")):
        # The existing admin command monitor still handles private bot replies.
        return True
    if data.get("is_dm"):
        return False
    identity = data.get("channel_visibility_id")
    # Legacy history lacks a key-bound identity and must not be reinterpreted
    # using a possibly replaced channel's name or slot.
    return bool(
        identity and identity == message_identity(conn, data.get("channel")) and identity in permitted_ids(conn)
    )


def encrypted_packet_view(data):
    """Keep radio metadata/ciphertext, never ship server-side plaintext via packets."""
    if isinstance(data, list):
        return [encrypted_packet_view(value) for value in data]
    if not isinstance(data, dict):
        return data
    plaintext_keys = {"text", "message", "content", "sender", "msg_timestamp", "senderPubKeyPrefix"}
    return {
        key: encrypted_packet_view(value)
        for key, value in data.items()
        if not key.startswith("decrypted") and not (data.get("decrypted") and key in plaintext_keys)
    }
