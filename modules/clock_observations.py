"""Clock evidence shared by scheduling, Contacts and Time.

SQLite CURRENT_TIMESTAMP (messages/sync logs) is UTC. Advertisement receipt
times are written by datetime.now() in the bot's configured local timezone.
An ACK is delivery evidence, never a clock measurement.
"""

import json
import math
import sqlite3
import time
from datetime import datetime, timezone

from .utils import get_config_timezone

MIN_CLOCK_EPOCH = 946684800  # Before 2000 is an unset device clock / uptime.


def parse_timestamp(value, *, config=None, utc=False):
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return float(value) if math.isfinite(value) else None
        date = datetime.fromisoformat(str(value).strip().replace('Z', '+00:00'))
        if date.tzinfo is None:
            tz = timezone.utc if utc else get_config_timezone(config)[0]
            date = tz.localize(date) if hasattr(tz, 'localize') else date.replace(tzinfo=tz)
        return date.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def utc_iso(value):
    stamp = parse_timestamp(value, utc=True)
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat() if stamp is not None else None


def latest_clock_samples(cursor, config):
    """Latest received clock-bearing packet per unique contact name.

    Keep the numeric drift even for an unset clock. Measurement displays use
    that value independently of the synchronization/verification state;
    timestamp_valid remains available to the clock-status column.
    """
    samples = {}

    def add(name, received, sender, source):
        received_at = parse_timestamp(received, config=config, utc=source == 'message')
        try:
            sender = int(sender)
        except (ValueError, TypeError, OverflowError):
            return
        if received_at is None or sender < 0:
            return
        old = samples.get(name)
        if old is not None and old['received_at'] >= received_at:
            return
        samples[name] = {'drift': abs(int(received_at) - sender), 'received_at': received_at,
                         'sender_timestamp': sender, 'source': source,
                         'timestamp_valid': MIN_CLOCK_EPOCH <= sender < 4102444800}

    cursor.execute('''
        SELECT m.sender_id, m.created_at, m.timestamp
        FROM message_stats m
        JOIN (SELECT sender_id, MAX(id) AS id FROM message_stats GROUP BY sender_id) latest
        ON latest.id = m.id
        WHERE m.timestamp IS NOT NULL
    ''')
    for row in cursor.fetchall():
        add(row[0], row[1], row[2], 'message')
    cursor.execute('SELECT name, last_heard, raw_advert_data FROM complete_contact_tracking')
    contacts = cursor.fetchall()
    names = {}
    for row in contacts:
        names[row[0]] = names.get(row[0], 0) + 1
        try:
            raw = json.loads(row[2]) if isinstance(row[2], str) else row[2]
        except (ValueError, TypeError):
            continue
        if isinstance(raw, dict):
            add(row[0], row[1], raw.get('advert_time'), 'advert')
    # A name-only message cannot prove which of two same-named devices sent it.
    return {name: sample for name, sample in samples.items() if names.get(name, 0) <= 1}


def observation_window(config):
    return max(0, config.getfloat('Clock_Sync_Admin', 'dashboard_check_window_hours', fallback=24)) * 3600


def latest_clock_attempts(cursor):
    try:
        cursor.execute('''
            SELECT public_key, success, error_message, sent_at FROM clock_sync_admin_log
            WHERE id IN (SELECT MAX(id) FROM clock_sync_admin_log GROUP BY public_key)
        ''')
        return {row[0]: {'success': bool(row[1]), 'error': row[2], 'at': utc_iso(row[3])}
                for row in cursor.fetchall()}
    except sqlite3.OperationalError as exc:
        if 'no such table' not in str(exc):
            raise
        return {}


def target_clock_status(sample, attempt, config, *, now=None):
    """Separate current clock evidence from the last transport attempt."""
    now = time.time() if now is None else now
    observed = sample['received_at'] if sample else None
    attempted = parse_timestamp(attempt.get('at'), utc=True) if attempt else None
    delivery = 'confirmed' if attempt and attempt.get('success') else 'unconfirmed' if attempt else 'none'
    firmware_error = bool(attempt and str(attempt.get('error') or '').startswith('ERR:'))
    after_attempt = attempted is not None and (observed is None or observed <= attempted)
    if firmware_error and after_attempt:
        state = 'rejected'
    elif delivery == 'confirmed' and after_attempt:
        state = 'awaiting_verification'
    elif sample is None:
        state = 'unknown'
    elif now - observed > observation_window(config):
        state = 'stale'
    elif not sample['timestamp_valid']:
        state = 'unset'
    elif sample['drift'] <= max(0, config.getint('Clock_Sync_Admin', 'min_sync_drift', fallback=60)):
        state = 'in_sync'
    elif sample['drift'] <= max(30, config.getint('Clock_Sync_Admin', 'dashboard_max_clock_drift_seconds', fallback=300)):
        state = 'warning'
    else:
        state = 'out_of_sync'
    return {'clock_state': state, 'sync_delivery_state': delivery,
            'clock_observed_at': observed,
            'clock_timestamp_valid': sample['timestamp_valid'] if sample else None,
            'clock_source': sample['source'] if sample else None,
            'clock_drift': sample['drift'] if sample else None}


def map_clock_fields(sample, config, *, now=None):
    """Annotate an already-visible relay with its latest measured clock.

    The map's existing node filters determine visibility. Old evidence is dated
    and marked stale instead of pretending an old bad clock has been corrected.
    A transport ACK cannot create or clear this measured-drift warning.
    """
    now = time.time() if now is None else now
    threshold = max(30, min(config.getint(
        'Clock_Sync_Admin', 'dashboard_max_clock_drift_seconds', fallback=300), 86400))
    return {
        'clock_drift_seconds': sample['drift'] if sample else None,
        'clock_signed_drift_seconds': sample['sender_timestamp'] - int(sample['received_at']) if sample else None,
        'clock_observed_at': sample['received_at'] if sample else None,
        'clock_drift_source': sample['source'] if sample else None,
        'clock_drift_exceeded': bool(sample and sample['drift'] > threshold),
        'clock_drift_threshold_seconds': threshold,
        'clock_observation_stale': bool(sample and now - sample['received_at'] > observation_window(config)),
    }
