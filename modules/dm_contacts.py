"""Resolve DM recipients against the radio, restoring only a verified node identity."""

from __future__ import annotations

import asyncio
import copy
import json
import math
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from meshcore import EventType

FULL_KEY = re.compile(r'[0-9a-f]{64}')
KEY_PREFIX = re.compile(r'[0-9a-f]{2,63}')
ROLES = {'companion': 1, 'repeater': 2, 'roomserver': 3, 'sensor': 4}


@dataclass
class ResolvedContact:
    contact: dict[str, Any] | None
    reason: str


class DmContactResolver:
    """One bounded preparation path, shared by all DM producers on a bot.

    A recently verified cache hit avoids duplicate reads. Otherwise use a
    key-specific read, one shared list refresh and one on-demand restoration. An
    incoming advert path is never copied into the outbound radio contact.
    """

    def __init__(self, bot: Any):
        self.bot = bot
        self.timeout = 5.0
        self.deadline = 20.0
        self._refresh_lock = asyncio.Lock()
        self._restore_lock = asyncio.Lock()
        self._last_refresh = -math.inf
        self._snapshot: dict[str, dict[str, Any]] | None = None
        self._locks: dict[str, list[Any]] = {}
        self._active: dict[str, int] = {}
        self._verified: dict[str, float] = {}

    def is_in_use(self, public_key: str) -> bool:
        return self._active.get(str(public_key).lower(), 0) > 0

    def invalidate(self, key: str) -> None:
        self._verified.pop(key.lower(), None)
        contacts = getattr(self.bot.meshcore, 'contacts', None)
        if isinstance(contacts, dict):
            for label, record in list(contacts.items()):
                if isinstance(record, dict) and self._key(record) == key.lower():
                    contacts.pop(label, None)

    def _radio_contacts(self, pending: bool = False) -> list[dict[str, Any]]:
        records = getattr(self.bot.meshcore, 'pending_contacts' if pending else 'contacts', {})
        if not isinstance(records, dict):
            return []
        return [r for r in records.values() if isinstance(r, dict)]

    @staticmethod
    def _key(record: dict[str, Any]) -> str:
        value = record.get('public_key')
        return value.strip().lower() if isinstance(value, str) and FULL_KEY.fullmatch(value.strip().lower()) else ''

    def _history(self, needle: str, by_key: bool) -> list[dict[str, Any]]:
        manager = getattr(self.bot, 'db_manager', None)
        if manager is None:
            return []
        query = ('lower(public_key) LIKE ?' if by_key else 'lower(name) = ?')
        params = (needle + '%' if by_key else needle,)
        rows = manager.execute_query(
            'SELECT public_key, name, role, raw_advert_data FROM complete_contact_tracking WHERE '
            + query + ' LIMIT 3', params,
        )
        return rows if isinstance(rows, list) else []

    def identify(self, identifier: str) -> tuple[str | None, dict[str, Any] | None, str]:
        if not isinstance(identifier, str) or not identifier.strip():
            return None, None, 'invalid_recipient'
        needle = identifier.strip().lower()
        if FULL_KEY.fullmatch(needle):
            rows = self._history(needle, True)
            row = next((r for r in rows if self._key(r) == needle), None)
            return needle, row, ''
        by_key = KEY_PREFIX.fullmatch(needle) is not None
        rows = self._history(needle, by_key)
        all_records = self._radio_contacts() + self._radio_contacts(pending=True) + rows
        matches: dict[str, dict[str, Any]] = {}
        for record in all_records:
            key = self._key(record)
            name = str(record.get('adv_name') or record.get('name') or '').strip().lower()
            if key and ((by_key and key.startswith(needle)) or (not by_key and name == needle)):
                matches[key] = record
        if len(matches) != 1:
            return None, None, 'ambiguous_contact' if matches else 'contact_missing'
        key = next(iter(matches))
        row = next((r for r in rows if self._key(r) == key), None)
        return key, row, ''

    async def _exchange(self, method, *args):
        async def exchange():
            session = getattr(self.bot, 'radio_session', None)
            if callable(session):
                async with session():
                    return await method(*args)
            return await method(*args)
        return await asyncio.wait_for(exchange(), timeout=self.timeout)

    async def _refresh(self) -> bool:
        async with self._refresh_lock:
            if time.monotonic() - self._last_refresh < 5.0:
                return self._snapshot is not None
            self._snapshot = None
            self._last_refresh = time.monotonic()
            method = getattr(self.bot.meshcore.commands, 'get_contacts', None)
            if method is None:
                return False
            result = await self._exchange(method)
            payload = getattr(result, 'payload', None)
            if getattr(result, 'type', None) != EventType.CONTACTS or not isinstance(payload, dict):
                return False
            if any(not isinstance(r, dict) or not self._key(r) for r in payload.values()):
                return False
            self._snapshot = {self._key(r): copy.deepcopy(r) for r in payload.values()}
            return True

    async def _read_key(self, key: str) -> dict[str, Any] | None:
        method = getattr(self.bot.meshcore.commands, 'get_contact_by_key', None)
        if method is None:
            return None
        result = await self._exchange(method, bytes.fromhex(key))
        candidate = getattr(result, 'payload', None)
        if (getattr(result, 'type', None) == EventType.NEXT_CONTACT
                and isinstance(candidate, dict) and self._key(candidate) == key):
            return copy.deepcopy(candidate)
        return None

    def _cache(self, contact: dict[str, Any]) -> None:
        contacts = getattr(self.bot.meshcore, 'contacts', None)
        if isinstance(contacts, dict):
            contacts[self._key(contact)] = contact
        self._verified[self._key(contact)] = time.monotonic()

    @staticmethod
    def restoration(row: dict[str, Any] | None, pending: dict[str, Any] | None, key: str) -> dict[str, Any] | None:
        """Require announcement data; never invent a contact from only a name/key."""
        if pending is not None:
            raw = pending
        elif row is not None:
            try:
                raw = json.loads(row.get('raw_advert_data') or '{}')
            except (TypeError, ValueError):
                return None
        else:
            return None
        if not isinstance(raw, dict) or DmContactResolver._key(raw) != key:
            return None
        try:
            name = raw.get('adv_name') or raw.get('name')
            native = all(k in raw for k in ('type', 'flags', 'last_advert', 'adv_lat', 'adv_lon'))
            if not native:
                # Parsed, signed RF adverts retained by the packet decoder.
                signature = raw.get('signature')
                if not isinstance(signature, str) or not re.fullmatch(r'[0-9a-fA-F]{128}', signature):
                    return None
            role = raw.get('type') if native else ROLES.get(str((row or {}).get('role', '')).lower())
            stamp = raw.get('last_advert') if native else raw.get('advert_time')
            flags = raw.get('flags', 0)
            lat = float(raw.get('adv_lat', 0) if native else raw.get('lat', 0) or 0)
            lon = float(raw.get('adv_lon', 0) if native else raw.get('lon', 0) or 0)
            if (not isinstance(name, str) or not name or len(name.encode('utf-8')) > 32
                    or type(role) is not int or role not in ROLES.values()
                    or type(stamp) is not int or not 0 < stamp < 2**32
                    or type(flags) is not int or not 0 <= flags <= 255
                    or not math.isfinite(lat) or not math.isfinite(lon)
                    or not -90 <= lat <= 90 or not -180 <= lon <= 180):
                return None
            return {'public_key': key, 'adv_name': name, 'type': role, 'flags': flags,
                    'last_advert': stamp, 'adv_lat': lat, 'adv_lon': lon,
                    'out_path': '', 'out_path_len': -1, 'out_path_hash_mode': 0}
        except (TypeError, ValueError, OverflowError):
            return None

    async def _resolve(self, key: str, row: dict[str, Any] | None) -> ResolvedContact:
        # A cleanup already in progress must finish before we claim this node.
        manager = getattr(self.bot, 'repeater_manager', None)
        purging: set[str] = getattr(manager, '_purge_inflight_keys', set())
        if isinstance(purging, set) and any(str(k).lower() == key for k in purging):
            return ResolvedContact(None, 'contact_cleanup_in_progress')
        cached = next((r for r in self._radio_contacts() if self._key(r) == key), None)
        if cached is not None and time.monotonic() - self._verified.get(key, -math.inf) < 2.0:
            return ResolvedContact(cached, 'radio_cache')
        try:
            contact = await self._read_key(key)
        except (asyncio.TimeoutError, TypeError, AttributeError):
            contact = None
        if contact is not None:
            self._cache(contact)
            return ResolvedContact(contact, 'radio_read')
        refreshed = await self._refresh()
        if refreshed:
            cached = (self._snapshot or {}).get(key)
            if cached is not None:
                self._cache(cached)
                return ResolvedContact(cached, 'radio_refresh')
        else:
            return ResolvedContact(None, 'contact_refresh_failed')
        pending = next((r for r in self._radio_contacts(pending=True) if self._key(r) == key), None)
        restored = self.restoration(row, pending, key) or self.restoration(row, None, key)
        if restored is None:
            return ResolvedContact(None, 'contact_data_incomplete')
        config = getattr(self.bot, 'config', None)
        if config is not None and config.get('Bot', 'auto_manage_contacts', fallback='bot').strip().lower() == 'false':
            return ResolvedContact(None, 'contact_restore_disabled')
        async with self._restore_lock:
            limit = getattr(manager, 'contact_limit', None)
            if type(limit) is not int or limit <= 0:
                return ResolvedContact(None, 'contact_capacity_unknown')
            if self._snapshot is None or len(self._snapshot) >= limit:
                return ResolvedContact(None, 'contact_table_full')
            method = getattr(self.bot.meshcore.commands, 'add_contact', None)
            if method is None:
                return ResolvedContact(None, 'contact_restore_unavailable')
            added = await self._exchange(method, restored)
            if getattr(added, 'type', None) != EventType.OK:
                payload = getattr(added, 'payload', {})
                full = isinstance(payload, dict) and (payload.get('error_code') == 3
                                                     or 'TABLE_FULL' in str(payload.get('code_string', '')).upper())
                return ResolvedContact(None, 'contact_table_full' if full else 'contact_restore_failed')
            # An OK only accepts the update. Read the specific key back before sending.
            confirmed = await self._read_key(key)
            if confirmed is None:
                return ResolvedContact(None, 'contact_restore_unconfirmed')
            self._cache(confirmed)
            if self._snapshot is not None:
                self._snapshot[key] = confirmed
            self.bot.logger.info('DM contact restored and verified | Key prefix: %s', key[:12])
            return ResolvedContact(confirmed, 'radio_restored')

    @asynccontextmanager
    async def use(self, identifier: str):
        key, row, reason = self.identify(identifier)
        if key is None and reason not in ('ambiguous_contact', 'invalid_recipient'):
            try:
                await self._refresh()
                key, row, reason = self.identify(identifier)
            except (asyncio.TimeoutError, TypeError, AttributeError):
                reason = 'contact_refresh_failed'
        if key is None:
            yield ResolvedContact(None, reason)
            return
        entry = self._locks.setdefault(key, [asyncio.Lock(), 0])
        entry[1] += 1
        self._active[key] = self._active.get(key, 0) + 1
        try:
            async with entry[0]:
                try:
                    result = await asyncio.wait_for(self._resolve(key, row), timeout=self.deadline)
                except asyncio.TimeoutError:
                    result = ResolvedContact(None, 'contact_resolution_timeout')
                except Exception:
                    self.bot.logger.warning('DM contact preparation failed | Key prefix: %s', key[:12])
                    result = ResolvedContact(None, 'contact_resolution_failed')
                yield result
        finally:
            self._active[key] -= 1
            if self._active[key] == 0:
                del self._active[key]
            entry[1] -= 1
            if entry[1] == 0:
                del self._locks[key]
