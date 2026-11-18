#!/usr/bin/env python3
"""
DX command - the farthest station heard by the repeater nearest to the bot.

The evidence is ``observed_paths``: for an advert, the first node of the path seen
on the wire is the gateway that first repeated it, so a row whose entry relay is
the repeater closest to the bot is a station that this repeater received. The
reception the bot made itself (``path_length`` 0) is the bot's own DX rather than
a repeater's, so it is left out.

Distances are straight-line (haversine) between the repeater and the station's
most recently advertised position, which is what "the farthest station" means on
an RF record: the link that closed, not the route it took afterwards.
"""

import re
from typing import Any, Optional

from ..models import MeshMessage
from .base_command import BaseCommand

# Roles that act as a gateway: a repeater, or a room server standing in for one.
# A prefix owns a reception only when it resolves to one node, so `_entry_relay_key`
# keeps the mesh graph's own collision rule rather than guessing.
_GATEWAY_ROLES = ("repeater", "roomserver")
_GATEWAY_ROLE_SQL = ", ".join("?" for _ in _GATEWAY_ROLES)
_MIN_NAME_LEN = 12
_MAX_NAME_LEN = 24


class DxCommand(BaseCommand):
    """Show the farthest stations received by the repeater nearest to the bot."""

    name = "dx"
    keywords = ["dx"]
    description = "Farthest station heard by the nearest repeater"
    requires_dm = False
    cooldown_seconds = 15
    category = "mesh"

    short_description = "Farthest station heard by the nearest repeater"
    usage = "dx [count] [days]"
    examples = ["dx", "dx 3", "dx 5 30d"]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self.dx_enabled = self.get_config_value("Dx_Command", "enabled", fallback=True, value_type="bool")
        self.dx_days = self.get_config_value("Dx_Command", "days", fallback=7, value_type="int")
        self.dx_limit = self.get_config_value("Dx_Command", "limit", fallback=5, value_type="int")

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        if not self.dx_enabled:
            return False
        return super().can_execute(message)

    def _parse_args(self, message: MeshMessage) -> tuple[int, int]:
        """Read the optional row count and window in days from the message."""
        prefix = (self.bot.config.get("Bot", "command_prefix", fallback="") or "").strip()
        content = message.content or ""
        keywords_alt = "|".join(re.escape(k) for k in [self.name] + self.keywords)
        if prefix:
            pattern = re.compile(rf"^{re.escape(prefix)}?(?:{keywords_alt})\s*(.*)", re.IGNORECASE)
        else:
            pattern = re.compile(rf"^(?:{keywords_alt})\s*(.*)", re.IGNORECASE)
        match = pattern.match(content)
        rest = match.group(1).strip() if match else ""

        count = max(1, min(int(self.dx_limit or 5), 10))
        days = max(1, int(self.dx_days or 7))
        for token in rest.split():
            if token.isdigit():
                count = max(1, min(int(token), 10))
            else:
                span = re.fullmatch(r"(?i)(\d{1,3})d", token)
                if span:
                    days = max(1, min(int(span.group(1)), 365))
        return count, days

    def _reference_position(self, message: MeshMessage) -> Optional[tuple[float, float]]:
        """Where "nearest" is measured from: the bot's own position, else the sender's."""
        from ..location import get_bot_lat_lon

        position = get_bot_lat_lon(self.bot, self.logger)
        if position:
            return position
        sender_key = (getattr(message, "sender_pubkey", None) or "").strip()
        if not sender_key:
            return None
        try:
            rows = self.bot.db_manager.execute_query(
                """
                SELECT latitude, longitude FROM complete_contact_tracking
                WHERE public_key = ?
                AND latitude IS NOT NULL AND longitude IS NOT NULL
                AND latitude != 0 AND longitude != 0
                ORDER BY COALESCE(last_advert_timestamp, last_heard) DESC LIMIT 1
                """,
                (sender_key,),
            )
        except Exception as e:  # noqa: BLE001 - a lookup failure is not an error worth raising
            self.logger.debug("DX: sender position lookup failed: %s", e)
            return None
        if rows:
            return (float(rows[0]["latitude"]), float(rows[0]["longitude"]))
        return None

    def _nearest_repeater(
        self, reference: tuple[float, float]
    ) -> Optional[dict[str, Any]]:
        """The gateway with a position closest to ``reference``, by straight-line distance."""
        lat, lon = reference
        try:
            rows = self.bot.db_manager.execute_query(
                f"""
                SELECT name, public_key, latitude, longitude,
                       6371 * 2 * ASIN(SQRT(
                           POWER(SIN(RADIANS(latitude - ?) / 2), 2) +
                           COS(RADIANS(?)) * COS(RADIANS(latitude)) *
                           POWER(SIN(RADIANS(longitude - ?) / 2), 2)
                       )) AS distance_km
                FROM complete_contact_tracking
                WHERE role IN ({_GATEWAY_ROLE_SQL})
                AND latitude IS NOT NULL AND longitude IS NOT NULL
                AND latitude != 0 AND longitude != 0
                ORDER BY distance_km ASC
                LIMIT 1
                """,
                (lat, lat, lon, *_GATEWAY_ROLES),
            )
        except Exception as e:  # noqa: BLE001 - a lookup failure is reported, not raised
            self.logger.warning("DX: repeater lookup failed: %s", e)
            return None
        if not rows:
            return None
        return rows[0]

    def _entry_relay_key(self, from_prefix: str, reference: tuple[float, float]) -> Optional[str]:
        """Which node a path's first prefix belongs to, reusing the mesh-path resolver.

        The resolver picks the plausible owner of a colliding prefix the way the mesh
        graph does (starred first, then the closest to ``reference``, then the most
        recently heard), so a prefix is only trusted when that owner is unambiguous.
        """
        from ..utils import _get_node_location_from_db

        try:
            resolved = _get_node_location_from_db(self.bot, from_prefix, reference)
        except Exception as e:  # noqa: BLE001 - a lookup failure only costs this row
            self.logger.debug("DX: prefix %s lookup failed: %s", from_prefix, e)
            return None
        if not resolved:
            return None
        return resolved[1]

    def _observed_dx_rows(self, repeater: dict[str, Any], days: int) -> list[dict[str, Any]]:
        """Adverts of the window whose entry relay is ``repeater``, newest position first."""
        try:
            rows = self.bot.db_manager.execute_query(
                """
                SELECT op.public_key, op.from_prefix, op.last_seen,
                       st.name, st.latitude, st.longitude
                FROM observed_paths op
                JOIN complete_contact_tracking st ON st.public_key = op.public_key
                WHERE op.packet_type = 'advert'
                AND op.public_key IS NOT NULL
                AND op.path_length > 0
                AND op.last_seen >= datetime('now', ?)
                AND lower(op.from_prefix) = lower(substr(?, 1, length(op.from_prefix)))
                AND st.latitude IS NOT NULL AND st.longitude IS NOT NULL
                AND NOT (st.latitude = 0 AND st.longitude = 0)
                ORDER BY COALESCE(st.last_advert_timestamp, st.last_heard) DESC,
                         op.last_seen DESC
                LIMIT 1000
                """,
                (f"-{int(days)} days", repeater["public_key"]),
            )
        except Exception as e:  # noqa: BLE001 - a lookup failure is reported, not raised
            self.logger.warning("DX: observed path query failed: %s", e)
            return []

        from ..utils import calculate_distance

        reference = (float(repeater["latitude"]), float(repeater["longitude"]))
        owner_cache: dict[str, Optional[str]] = {}
        stations: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = str(row["public_key"])
            if key in stations:
                # Already kept with this station's newest position; only later paths are left.
                continue
            prefix = str(row["from_prefix"])
            if prefix not in owner_cache:
                owner_cache[prefix] = self._entry_relay_key(prefix, reference)
            if owner_cache[prefix] != repeater["public_key"]:
                continue
            stations[key] = {
                "name": row["name"],
                "distance_km": calculate_distance(
                    reference[0], reference[1], float(row["latitude"]), float(row["longitude"])
                ),
                "last_seen": row["last_seen"],
            }
        return sorted(stations.values(), key=lambda item: item["distance_km"], reverse=True)

    @staticmethod
    def _clip(text: str, budget: int) -> str:
        """Cut to ``budget`` UTF-8 bytes without splitting a character."""
        if budget <= 0:
            return ""
        encoded = text.encode("utf-8")
        if len(encoded) <= budget:
            return text
        return encoded[:budget].decode("utf-8", "ignore").rstrip()

    @staticmethod
    def _distance_text(distance_km: float) -> str:
        """One distance: metres under a kilometre, one decimal under ten, else whole km."""
        if distance_km < 1:
            return f"{distance_km * 1000:.0f} m"
        if distance_km < 10:
            return f"{distance_km:.1f} km"
        return f"{distance_km:.0f} km"

    @classmethod
    def _row_overhead(cls, rank: int, distance_km: float) -> int:
        """Bytes a row costs apart from its name, at that rank and distance."""
        return len(f"{rank}.  {cls._distance_text(distance_km)}")

    @classmethod
    def _row_text(cls, rank: int, name: str, distance_km: float, name_len: int) -> str:
        """One row: rank, name, straight-line distance. No date: the frame has no room."""
        return f"{rank}. {cls._clip(name, name_len)} {cls._distance_text(distance_km)}"

    def _format(
        self,
        repeater: dict[str, Any],
        stations: list[dict[str, Any]],
        count: int,
        days: int,
        budget: int,
    ) -> str:
        """Header plus as many rows as the frame allows, keeping the farthest ones."""
        picked = stations[:count]
        header = f"DX {repeater['name']} {self._distance_text(repeater['distance_km'])} {days} j"
        if not picked:
            return self._clip(header, budget)

        # Each row costs its rank, ". ", the name, a space, its distance and the newline it
        # ends with. Names are shortened before rows are dropped - never below
        # _MIN_NAME_LEN - and rows go least far first, so the record always survives.
        for rows_in_reply in range(len(picked), 0, -1):
            rows = picked[:rows_in_reply]
            free = budget - len(header.encode("utf-8")) - (rows_in_reply - 1)
            overheads = [
                self._row_overhead(rank, item["distance_km"])
                for rank, item in enumerate(rows, start=1)
            ]
            name_len = min(_MAX_NAME_LEN, (free - sum(overheads)) // rows_in_reply)
            while name_len >= _MIN_NAME_LEN:
                lines = [
                    self._row_text(rank, item["name"], item["distance_km"], name_len)
                    for rank, item in enumerate(rows, start=1)
                ]
                reply = "\n".join([header, *lines])
                if len(reply.encode("utf-8")) <= budget:
                    return reply
                name_len -= 1

        name_len = max(1, budget - self._row_overhead(1, picked[0]["distance_km"]))
        return self._clip(self._row_text(1, picked[0]["name"], picked[0]["distance_km"], name_len),
                          budget)

    async def execute(self, message: MeshMessage) -> bool:
        count, days = self._parse_args(message)

        reference = self._reference_position(message)
        if reference is None:
            return await self.send_response(
                message,
                "Position inconnue : renseigne bot_latitude/bot_longitude dans [Bot].",
            )

        repeater = self._nearest_repeater(reference)
        if repeater is None:
            return await self.send_response(message, "Aucun répéteur avec position GPS connu.")

        stations = self._observed_dx_rows(repeater, days)
        if not stations:
            return await self.send_response(
                message,
                f"Aucune station reçue par {self._clip(repeater['name'], 24)} ces {days} derniers jours.",
            )

        response = self._format(
            repeater, stations, count, days, self.get_max_message_length(message)
        )
        return await self.send_response(message, response)
