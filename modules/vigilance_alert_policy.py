"""Changes and daily reminders, based on the last successfully transmitted alert."""

import hashlib
import json
import math
from typing import Any

COLORS = {'Vert', 'Jaune', 'Orange', 'Rouge'}


def fingerprint(info: dict[str, Any]) -> str:
    """Ignore publication timestamps; a new edition is not necessarily a change."""
    if info.get('color') not in COLORS:
        raise ValueError('Unknown vigilance level')
    phenomena = info.get('phenomena')
    if phenomena is None:
        details: Any = ' '.join(str(info.get('summary', '')).split())
    else:
        details = sorted((str(item['id']), item['color']) for item in phenomena)
    raw = json.dumps([info['color'], details], ensure_ascii=True, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


class VigilanceAlertPolicy:
    def __init__(self, db_manager: Any, logger: Any, channel: str, scope: str | None = '',
                 *, reminder_seconds: int = 86400) -> None:
        self.db = db_manager
        self.logger = logger
        target = channel.strip().casefold() + '\0' + str(scope or '').strip().casefold()
        self.namespace = 'vigilance.alert.v1.' + hashlib.sha256(target.encode()).hexdigest()[:20]
        self.reminder_seconds = reminder_seconds
        self.states: dict[str, dict[str, Any] | None] = {}

    def _key(self, code: str) -> str:
        return self.namespace + '.' + code

    def state(self, code: str) -> dict[str, Any] | None:
        if code not in self.states:
            state = None
            try:
                raw = self.db.get_metadata(self._key(code)) if self.db is not None else None
                if isinstance(raw, str) and raw:
                    candidate = json.loads(raw)
                    if (isinstance(candidate, dict) and candidate.get('color') in COLORS
                            and isinstance(candidate.get('fingerprint'), str)
                            and len(candidate['fingerprint']) == 64
                            and type(candidate.get('sent_at')) in (int, float)
                            and math.isfinite(candidate['sent_at']) and candidate['sent_at'] > 0):
                        state = candidate
                    else:
                        raise ValueError('Invalid vigilance alert state')
            except Exception:
                self.logger.warning('Could not load vigilance alert state for department %s', code)
            self.states[code] = state
        return self.states[code]

    def reason(self, code: str, info: dict[str, Any], levels: list[str], now: float) -> str | None:
        signature = fingerprint(info)
        previous = self.state(code)
        color = info['color']
        if color == 'Vert':
            return 'clear' if previous and previous['color'] != 'Vert' else None
        if color not in levels:
            return None
        if previous is None or previous['color'] == 'Vert':
            return 'new'
        if signature != previous['fingerprint']:
            return 'change'
        if now - previous['sent_at'] >= self.reminder_seconds or previous['sent_at'] > now + 300:
            return 'reminder'
        return None

    def record_sent(self, code: str, info: dict[str, Any], now: float) -> None:
        state = {'color': info['color'], 'fingerprint': fingerprint(info), 'sent_at': now}
        self.states[code] = state
        if self.db is None:
            self.logger.warning('No database to persist vigilance alert state')
            return
        raw = json.dumps(state, sort_keys=True)
        try:
            self.db.set_metadata(self._key(code), raw)
            if self.db.get_metadata(self._key(code)) != raw:
                raise OSError('Vigilance alert state was not persisted')
        except Exception:
            self.logger.warning('Could not persist vigilance alert state for department %s', code)
