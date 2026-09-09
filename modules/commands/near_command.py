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

    def _parse_args(self, message: MeshMessage) -> tuple[int, str | None, str | None]:
        """Parse optional count, role, and target node prefix from the message."""
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
        target_prefix = None
        tokens = rest.split()
        for t in tokens:
            if t.isdigit():
                count = max(1, min(20, int(t)))
            elif t in ("repeater", "sensor", "companion", "roomserver"):
                role = t
            elif re.match(r"^[0-9A-Fa-f]{4}$", t):
                target_prefix = t.upper()
        return count, role, target_prefix

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

    def _get_node_position_by_prefix(self, node_prefix: str) -> tuple[float, float] | None:
        """Look up a node's GPS position by its 4-char key prefix."""
        try:
            with self.bot.db_manager.connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT latitude, longitude FROM complete_contact_tracking WHERE public_key LIKE ? AND latitude IS NOT NULL AND longitude IS NOT NULL AND latitude != 0 LIMIT 1",
                    (f"{node_prefix.lower()}%",),
                )
                row = cursor.fetchone()
                if row:
                    return (float(row[0]), float(row[1]))
        except Exception:
            pass
        return None

    async def execute(self, message: MeshMessage) -> bool:
        count, role, target_prefix = self._parse_args(message)

        pos = None
        if target_prefix:
            pos = self._get_node_position_by_prefix(target_prefix)
            if not pos:
                return await self.send_response(message, f"Node {target_prefix} sans GPS trouvé.")
        else:
            pos = self._get_sender_position(message)
            if not pos:
                return await self.send_response(message, "Position GPS introuvable pour calculer les distances.")

        lat, lon = pos
        limit = count

        sender_key = (message.sender_id or "").strip()
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
                if sender_key:
                    sql += f" AND public_key != '{sender_key}'"
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

        # Priority: maximize station count. Truncate names to fit.
        # Each line: name + ": X.X km" (8) worst case. Total: n*(name_len+8) + (n-1) <= max_length
        # Max possible count with min_name_len: (max_length+1) // (min_name_len + 9)
        max_possible = (max_length + 1) // (min_name_len + 9)
        target_count = min(len(rows), max(limit, max_possible)) if limit > 1 else max_possible
        target_count = min(target_count, len(rows), max_possible)

        # Find name_len that fits target_count lines
        # n*(name_len+8) + (n-1) <= max_length → name_len <= (max_length+1)/n - 9
        name_len = (max_length + 1) // target_count - 9
        name_len = max(min_name_len, min(name_len, 30))

        lines = []
        for name, dist in rows[:target_count]:
            display_name = name[:name_len] if len(name) > name_len else name
            if dist < 1.0:
                lines.append(f"{display_name}: {dist * 1000:.0f} m")
            else:
                lines.append(f"{display_name}: {dist:.1f} km")

        response = "\n".join(lines)
        if len(response) > max_length:
            # Shrink name_len and retry
            name_len = min_name_len
            lines = []
            for name, dist in rows[:target_count]:
                display_name = name[:name_len] if len(name) > name_len else name
                if dist < 1.0:
                    lines.append(f"{display_name}: {dist * 1000:.0f} m")
                else:
                    lines.append(f"{display_name}: {dist:.1f} km")
            response = "\n".join(lines)
            if len(response) > max_length:
                response = response[:max_length]

        return await self.send_response(message, response)
