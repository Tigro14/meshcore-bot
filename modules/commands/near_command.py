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
        return self.near_enabled and self.can_use_service(message, skip_channel_check)

    def can_use_service(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        return super().can_use_service(message, skip_channel_check=skip_channel_check)

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
        from ..near_location import resolve_origin
        count, role, target_prefix = self._parse_args(message)
        label = ""
        origin_key = ""
        try:
            with self.bot.db_manager.connection() as conn:
                if target_prefix:
                    pos = self._get_node_position_by_prefix(target_prefix)
                    if not pos:
                        return await self.send_response(message, f"Node {target_prefix} sans GPS trouvé.")
                else:
                    origin, error = resolve_origin(conn, message)
                    if origin is None:
                        return await self.send_response(message, error)
                    pos = (origin.latitude, origin.longitude)
                    label, origin_key = origin.label, origin.public_key
                lat, lon = pos
                sql = (
                    "SELECT name, 6371*2*ASIN(MIN(1,SQRT("
                    "POWER(SIN(RADIANS(latitude-?)/2),2)+"
                    "COS(RADIANS(?))*COS(RADIANS(latitude))*"
                    "POWER(SIN(RADIANS(longitude-?)/2),2)))) AS dist "
                    "FROM complete_contact_tracking WHERE latitude BETWEEN -90 AND 90 "
                    "AND longitude BETWEEN -180 AND 180 AND NOT (latitude=0 AND longitude=0)"
                )
                params = [lat, lat, lon]
                for key in {origin_key, message.sender_pubkey or ""} - {""}:
                    sql += " AND public_key != ?"
                    params.append(key)
                if role:
                    sql += " AND role = ?"
                    params.append(role)
                sql += " ORDER BY dist ASC LIMIT ?"
                params.append(count)
                rows = conn.execute(sql, params).fetchall()
        except Exception:
            self.logger.exception("Near origin/distance lookup failed")
            return await self.send_response(message, "Les données de localisation sont temporairement indisponibles.")
        if not rows:
            return await self.send_response(message, (label + " " if label else "") + "Aucun autre nœud géolocalisé correspondant.")
        budget = self.get_max_message_length(message)
        lines = [label] if label else []
        for name, distance in rows:
            name = str(name or "Sans nom").replace("\n", " ")
            name = name.encode("utf-8")[:28].decode("utf-8", errors="ignore")
            value = f"{distance * 1000:.0f} m" if distance < 1 else f"{distance:.1f} km"
            line = f"{name}: {value}"
            if len("\n".join(lines + [line]).encode("utf-8")) > budget:
                break
            lines.append(line)
        return await self.send_response(message, "\n".join(lines))
