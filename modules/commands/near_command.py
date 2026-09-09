#!/usr/bin/env python3
"""
Near command - find nearest repeaters/nodes by GPS distance.
"""

import re
from typing import Any

from ..models import MeshMessage
from .base_command import BaseCommand


class NearCommand(BaseCommand):
    """Find the nearest repeaters or nodes based on sender GPS position."""

    name = "near"
    keywords = ["near", "n", "proche"]
    description = "Show nearest repeaters/nodes by distance"
    requires_dm = False
    cooldown_seconds = 10
    category = "mesh"

    short_description = "Nearest repeaters by distance"
    usage = "near [count] [role]"
    examples = ["near", "near 10", "near 5 sensor"]

    def __init__(self, bot: Any):
        super().__init__(bot)
        self.near_enabled = self.get_config_value("Near_Command", "enabled", fallback=True, value_type="bool")

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        if not self.near_enabled:
            return False
        return super().can_execute(message)

    def _parse_args(self, message: MeshMessage) -> tuple[int, str | None]:
        """Parse optional count and role from the message."""
        prefix = (self.bot.config.get("Bot", "command_prefix", fallback="") or "").strip()
        content = message.content or ""
        keywords_alt = "|".join(re.escape(k) for k in [self.name] + self.keywords)
        if prefix:
            pattern = re.compile(rf"^{re.escape(prefix)}?(?:{keywords_alt})\s*(.*)", re.IGNORECASE)
        else:
            pattern = re.compile(rf"^(?:{keywords_alt})\s*(.*)", re.IGNORECASE)
        match = pattern.match(content)
        rest = match.group(1).strip() if match else ""

        count = 5
        role = None
        tokens = rest.split()
        for t in tokens:
            if t.isdigit():
                count = max(1, min(20, int(t)))
            elif t in ("repeater", "sensor", "companion", "roomserver"):
                role = t
        return count, role

    def _get_sender_position(self, message: MeshMessage) -> tuple[float, float] | None:
        """Look up the sender's GPS position."""
        sender_id = message.sender_id or ""
        if not sender_id:
            return None
        try:
            with self.bot.db_manager.connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT latitude, longitude FROM complete_contact_tracking WHERE name = ? AND latitude IS NOT NULL AND longitude IS NOT NULL AND latitude != 0 LIMIT 1",
                    (sender_id,),
                )
                row = cursor.fetchone()
                if not row:
                    cursor.execute(
                        "SELECT latitude, longitude FROM complete_contact_tracking WHERE public_key = ? AND latitude IS NOT NULL AND longitude IS NOT NULL AND latitude != 0 LIMIT 1",
                        (sender_id,),
                    )
                    row = cursor.fetchone()
                if row:
                    return (float(row[0]), float(row[1]))
        except Exception:
            pass
        return None

    async def execute(self, message: MeshMessage) -> bool:
        count, role = self._parse_args(message)

        pos = self._get_sender_position(message)
        if not pos:
            return await self.send_response(message, "Position GPS introuvable pour calculer les distances.")

        lat, lon = pos
        limit = count

        try:
            with self.bot.db_manager.connection() as conn:
                cursor = conn.cursor()
                sql = (
                    "SELECT name, "
                    "6371*2*ASIN(SQRT("
                    f"POWER(SIN(RADIANS(latitude-{lat:.5f})/2),2)+"
                    f"COS(RADIANS({lat:.5f}))*COS(RADIANS(latitude))*"
                    f"POWER(SIN(RADIANS(longitude-{lon:.5f})/2),2)"
                    ")) AS dist "
                    "FROM complete_contact_tracking "
                    "WHERE latitude IS NOT NULL AND longitude IS NOT NULL AND latitude != 0"
                )
                if role:
                    sql += f" AND role = '{role}'"
                sql += f" ORDER BY dist ASC LIMIT {max(limit, 20)}"
                cursor.execute(sql)
                rows = cursor.fetchall()
        except Exception as e:
            self.logger.warning(f"Near command query error: {e}")
            return await self.send_response(message, f"Erreur query: {e}")

        if not rows:
            label = f" {role}" if role else " répéteur"
            return await self.send_response(message, f"Aucun{label} avec GPS trouvé.")

        max_length = self.get_max_message_length(message)
        min_name_len = 12
        # Each line costs: name_len + ": X.X km" (8) or ": XXX m" (7) → use 8 worst case
        # Total for n lines: n * (name_len + 8) + (n-1) newlines <= max_length
        # Solve for max n with a given name_len:
        # n * (name_len + 8) + n - 1 <= max_length
        # n * (name_len + 9) <= max_length + 1
        # n <= (max_length + 1) / (name_len + 9)

        # Try to fit as many rows as possible, starting with full name length
        # and reducing name length down to min_name_len
        best_lines: list[str] = []
        for name_len in range(30, min_name_len - 1, -1):
            max_n = (max_length + 1) // (name_len + 9)
            if max_n < 1:
                continue
            count = min(len(rows), max_n)
            lines = []
            for name, dist in rows[:count]:
                display_name = name[:name_len] if len(name) > name_len else name
                if dist < 1.0:
                    lines.append(f"{display_name}: {dist * 1000:.0f} m")
                else:
                    lines.append(f"{display_name}: {dist:.1f} km")
            response = "\n".join(lines)
            if len(response) <= max_length:
                best_lines = lines
                break

        if not best_lines:
            # Fallback: use min_name_len
            max_n = (max_length + 1) // (min_name_len + 9)
            count = min(len(rows), max(1, max_n))
            for name, dist in rows[:count]:
                display_name = name[:min_name_len] if len(name) > min_name_len else name
                if dist < 1.0:
                    best_lines.append(f"{display_name}: {dist * 1000:.0f} m")
                else:
                    best_lines.append(f"{display_name}: {dist:.1f} km")

        response = "\n".join(best_lines)
        if len(response) > max_length:
            response = response[:max_length]

        return await self.send_response(message, response)
