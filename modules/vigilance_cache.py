"""Last complete vigilance bulletins and acknowledged availability notices.

Cached information is always historical when the source cannot be reached.
It is never fed back into the live alert policy.
"""

import json
import math
import time
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from modules.vigilance_alert_policy import COLORS
from modules.vigilance_periods import day_label, risk_lines, split_key

MAX_UNDATED_VALIDITY_AGE = 86400


def publication_time(info: dict[str, Any]) -> float:
    value = info.get("issued_at")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
        return float(value)
    stamp = info.get("bulletin_date")
    if isinstance(stamp, str):
        stamp = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if not isinstance(stamp, datetime):
        raise ValueError("Missing bulletin date")
    return stamp.replace(tzinfo=timezone.utc).timestamp() if stamp.tzinfo is None else stamp.timestamp()


def usable(info: dict[str, Any], now: float) -> bool:
    try:
        issued = publication_time(info)
        end = info.get("valid_until")
        if info.get("color") not in COLORS or not 0 < issued <= now + 300:
            return False
        if end is None:
            return now - issued < MAX_UNDATED_VALIDITY_AGE
        return (
            isinstance(end, (int, float))
            and not isinstance(end, bool)
            and math.isfinite(end)
            and end > now
            and end > issued
        )
    except (ValueError, TypeError, OverflowError, OSError):
        return False


class VigilanceCache:
    def __init__(self, db: Any, logger: Any) -> None:
        self.db = db
        self.logger = logger
        self.memory: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _key(code: str) -> str:
        return "vigilance.bulletin.v1." + code

    def get(self, code: str) -> dict[str, Any] | None:
        record = self.memory.get(code)
        try:
            raw = self.db.get_metadata(self._key(code)) if self.db is not None else None
            if isinstance(raw, str) and raw:
                candidate = json.loads(raw)
                info = candidate["info"]
                _, day = split_key(code)
                if (
                    candidate.get("version") != 1
                    or candidate.get("department") != code
                    or info.get("color") not in COLORS
                    or type(candidate.get("fetched_at")) not in (int, float)
                    or not math.isfinite(candidate["fetched_at"])
                    or candidate["fetched_at"] <= 0
                    or (day is not None and info.get('period_date') != day)
                ):
                    raise ValueError("Invalid cached bulletin")
                issued = publication_time(info)
                if not math.isfinite(issued) or not 0 < issued <= time.time() + 300:
                    raise ValueError("Invalid cached publication date")
                datetime.fromtimestamp(issued, timezone.utc)
                until = info.get("valid_until")
                if until is not None and (
                    type(until) not in (int, float) or not math.isfinite(until) or until <= issued
                ):
                    raise ValueError("Invalid cached validity")
                if record is None or (issued, candidate["fetched_at"]) >= (
                    publication_time(record["info"]),
                    record["fetched_at"],
                ):
                    record = candidate
                    self.memory[code] = candidate
        except Exception:
            self.logger.warning("Could not read cached vigilance bulletin for dept %s", code)
        return deepcopy(record) if record is not None else None

    def put(self, code: str, info: dict[str, Any], now: float) -> bool:
        _, day = split_key(code)
        if day is not None and info.get('period_date') != day:
            return False
        if not usable(info, now):
            return False
        previous = self.get(code)
        issued = publication_time(info)
        if previous is not None and issued < publication_time(previous["info"]):
            self.logger.warning("Ignoring older vigilance bulletin for dept %s", code)
            return False
        data = deepcopy(info)
        data["issued_at"] = issued
        if isinstance(data.get("bulletin_date"), datetime):
            data["bulletin_date"] = data["bulletin_date"].isoformat()
        record = {"version": 1, "department": code, "fetched_at": now, "info": data}
        try:
            raw = json.dumps(record, ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError):
            self.logger.warning("Could not serialize vigilance bulletin for dept %s", code)
            return False
        self.memory[code] = record
        try:
            if self.db is not None:
                self.db.set_metadata(self._key(code), raw)
                if self.db.get_metadata(self._key(code)) != raw:
                    raise OSError("Bulletin cache was not persisted")
        except Exception:
            self.logger.warning("Could not persist vigilance bulletin for dept %s", code)
        return True

    def status(self, code: str, now: float) -> str:
        record = self.get(code)
        if record is None:
            return "missing"
        until = record["info"].get("valid_until")
        if type(until) in (int, float) and until <= now:
            return "expired"
        if until is None and now - publication_time(record["info"]) >= MAX_UNDATED_VALIDITY_AGE:
            return "old"
        return "unverified"

    def historical_reply(
        self,
        code: str,
        now: float,
        tz: Any,
        *,
        phenomenon_names: dict[str, str] | None = None,
        department_label: str | None = None,
    ) -> str:
        department, day = split_key(code)
        label = department_label or f"Dept {department}"
        if day is not None:
            label += ' - ' + day_label(day, now)
        record = self.get(code)
        if record is None:
            return f"Vigilance non actualisable - {label}. Aucun bulletin conservé. Situation actuelle inconnue."
        info = record["info"]
        status = self.status(code, now)
        expired = status == "expired"
        title = {"expired": "Bulletin périmé", "old": "Bulletin ancien"}.get(status, "Bulletin non actualisé")
        issued = datetime.fromtimestamp(publication_time(info), tz).strftime("%d/%m/%Y à %H:%M")
        lines = [f"{title} - {label}", f"Dernier niveau connu : {info['color'].lower()} ({issued})."]
        lines.extend(risk_lines(info, phenomenon_names or {}))
        until = info.get("valid_until")
        if type(until) in (int, float):
            end = datetime.fromtimestamp(until, tz).strftime("%d/%m/%Y à %H:%M")
            lines.append(f"Validité {'échue' if expired else 'annoncée'} : {end}.")
        lines.append("Situation actuelle inconnue.")
        return "\n".join(lines)


class AvailabilityNotices:
    """One notice per outage/expiry transition, only acknowledged after transmission."""

    def __init__(self, db: Any, logger: Any, namespace: str) -> None:
        self.db = db
        self.logger = logger
        self.key = namespace + ".availability"
        self.last: dict[str, str] | None = None
        try:
            raw = db.get_metadata(self.key) if db is not None else None
            if isinstance(raw, str) and raw:
                state = json.loads(raw)
                if not isinstance(state, dict) or not all(
                    isinstance(k, str) and v in {"missing", "expired", "old", "unverified"} for k, v in state.items()
                ):
                    raise ValueError("Invalid availability state")
                self.last = state
        except Exception:
            self.logger.warning("Could not read vigilance availability state")

    def due(self, statuses: dict[str, str]) -> bool:
        return statuses != (self.last or {})

    def record_sent(self, statuses: dict[str, str]) -> None:
        self.last = dict(statuses)
        try:
            if self.db is not None:
                raw = json.dumps(statuses, sort_keys=True)
                self.db.set_metadata(self.key, raw)
                if self.db.get_metadata(self.key) != raw:
                    raise OSError("Availability state was not persisted")
        except Exception:
            self.logger.warning("Could not persist vigilance availability state")
