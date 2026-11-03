#!/usr/bin/env python3
"""
Vigilance Météo-France command for the MeshCore Bot
Provides current weather vigilance status for a French department
"""

import asyncio
from typing import Optional

from ..models import MeshMessage
from ..service_plugins.vigilance_service import fetch_vigilance
from .base_command import BaseCommand


class VigilanceCommand(BaseCommand):
    """Handles Vigilance Météo-France queries"""

    # Plugin metadata
    render_safe = True
    name = "vigi"
    keywords = ['vigi', 'vigilance', 'alert', 'alerte']
    description = "Get Météo-France vigilance status for a French department"
    category = "weather"
    cooldown_seconds = 10
    requires_internet = True

    # Documentation
    short_description = "Get Météo-France weather vigilance alerts (France only)"
    usage = "vigi [department_number]"
    examples = ["vigi", "vigi 75", "vigi 25"]
    parameters = [
        {
            "name": "department",
            "description": "French department number (optional; defaults to configured dept or 75)",
        }
    ]

    # Web-viewer settings schema
    settings_schema = [
        {
            "key": "default_department",
            "label": "Default department",
            "type": "str",
            "default": "75",
            "help": "Default French department number for vigi command (e.g. 75 for Paris).",
        },
    ]

    async def execute(self, message: MeshMessage) -> bool:
        """Execute the vigilance command.

        Fetches current Météo-France vigilance for a department and sends the status.

        Args:
            message: The message that triggered the command.

        Returns:
            bool: True if executed successfully, False otherwise.
        """
        try:
            # Parse department from arguments, fall back to config or default
            dept = self._parse_department(message.text)

            # Fetch vigilance data in executor to avoid blocking
            loop = asyncio.get_event_loop()
            vigilance_data = await loop.run_in_executor(
                None, lambda: fetch_vigilance(dept)
            )

            # Format response
            response = self._format_response(dept, vigilance_data)
            return await self.send_response(message, response)

        except Exception as e:
            error_msg = f"⚠️ Erreur vigilance: {str(e)}"
            await self.send_response(message, error_msg)
            return False

    def _parse_department(self, text: str) -> str:
        """Parse department number from command text.

        Falls back to configured default, then '75'.

        Args:
            text: The command text (e.g. "vigi 25")

        Returns:
            str: The department number
        """
        parts = text.strip().split()
        if len(parts) > 1:
            dept = parts[1].strip()
            if dept.isdigit() and 1 <= len(dept) <= 3:
                return dept.zfill(2)  # Pad to 2 digits (01, 02, etc.)

        # Fall back to configured default
        default = self.bot.config.get(
            "Vigilance_Service", "department", fallback="75"
        ).strip()
        return default

    def _format_response(self, dept: str, data: dict) -> str:
        """Format a compact vigilance alert message.

        Args:
            dept: The department number
            data: Vigilance data dict with keys: color, summary, bulletin_date, url

        Returns:
            str: Formatted message (LoRa-friendly, ≤ 180 chars)
        """
        color = data.get("color", "Vert")
        emoji = {"Vert": "✅", "Jaune": "⚠️", "Orange": "🟠", "Rouge": "🔴"}.get(
            color, "🌦️"
        )

        # Compact format for mesh (LoRa)
        lines = [f"{emoji} Vigilance Dept {dept}: {color}"]

        summary = data.get("summary", "")
        if summary and ":" in summary:
            # Extract first phenomenon line if available
            for part in summary.split("\n")[1:3]:
                stripped = part.strip()
                if stripped.startswith("-"):
                    lines.append(stripped[2:])  # Remove "- " prefix
                    break

        # Limit total length for mesh
        result = "\n".join(lines)
        if len(result) > 180:
            result = result[:177] + "..."

        return result

    def get_help_text(self) -> str:
        """Get help text for this command.

        Returns:
            str: The help text for this command.
        """
        return (
            "Get Météo-France weather vigilance status.\n"
            "Usage: vigi [department_number]\n"
            "Examples:\n"
            "  vigi        — uses configured department\n"
            "  vigi 75     — Paris\n"
            "  vigi 25     — Doubs\n"
            "Levels: Vert (✅) = no alert, Jaune (⚠️) = yellow, Orange (🟠) = orange, Rouge (🔴) = red"
        )
