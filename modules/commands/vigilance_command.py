#!/usr/bin/env python3
"""
Vigilance Météo-France command for the MeshCore Bot
Provides current weather vigilance status for a French department
"""

import asyncio

from ..models import MeshMessage
from ..service_plugins.vigilance_service import (
    _REQUEST_DELAY_SECONDS,
    fetch_vigilance,
    normalize_department,
)
from .base_command import BaseCommand


class VigilanceCommand(BaseCommand):
    """Handles Vigilance Météo-France queries"""

    # Plugin metadata
    render_safe = True
    name = "vigi"
    # 'alert' and 'alerte' are not used here: they belong to alert_command, and two
    # plugins claiming the same keyword resolve in load order.
    keywords = ['vigi', 'vigilance']
    description = "Get Météo-France vigilance status for a French department"
    category = "weather"
    cooldown_seconds = 10
    requires_internet = True

    # Documentation
    short_description = "Get Météo-France weather vigilance alerts (France only)"
    usage = "vigi [department_number]"
    examples = ["vigi", "vigi 75", "vigi 95", "vigi 2A"]
    parameters = [
        {
            "name": "department",
            "description": "French department number (optional; defaults to the first configured dept or 75)",
        }
    ]

    # No settings_schema: the department is owned by [Vigilance_Service], so the
    # command and the monitor cannot end up configured with two different values.

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
            _trigger, args = self.split_trigger_and_args(message.content)
            dept = self._parse_department(args)
            api_key = str(self.get_config_value("Vigilance_Service", "api_key", fallback="")).strip()
            delay = float(self.get_config_value(
                "Vigilance_Service", "request_delay", fallback=_REQUEST_DELAY_SECONDS,
                value_type="float",
            ))

            # Fetch vigilance data in executor to avoid blocking
            loop = asyncio.get_event_loop()
            vigilance_data = await loop.run_in_executor(
                None, lambda: fetch_vigilance(dept, api_key, delay=delay)
            )

            # Format response
            response = self._format_response(dept, vigilance_data)
            return await self.send_response(message, response)

        except Exception as e:
            error_msg = f"⚠️ Erreur vigilance: {str(e)}"
            await self.send_response(message, error_msg)
            return False

    def _parse_department(self, args: str) -> str:
        """Parse department code from the arguments after the trigger keyword.

        Accepts the numeric codes (``95``) and the Corsican ones (``2a``/``2b``),
        which Météo-France writes as ``2A``/``2B``. Falls back to the configured
        department, then '75'. When the configuration lists several, the first one is
        used: answering for all of them at once would not fit the RF budget.

        Args:
            args: Argument text (e.g. "25" for "vigi 25")

        Returns:
            str: The department code
        """
        dept = args.strip()
        if dept.isalnum() and 1 <= len(dept) <= 3:
            return dept.zfill(2) if dept.isdigit() else dept.upper()

        # Fall back to the configured department, which may be a comma-separated list
        configured = str(
            self.get_config_value("Vigilance_Service", "department", fallback="75")
        ).strip()
        return normalize_department(configured.split(',')[0])

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

    def get_help_text(self, message: MeshMessage | None = None) -> str:
        """Get help text for this command.

        Returns:
            str: The help text for this command.
        """
        return (
            "Get Météo-France weather vigilance status.\n"
            "Usage: vigi [department_number]\n"
            "Examples:\n"
            "  vigi        — uses the configured department (the first when several)\n"
            "  vigi 75     — Paris\n"
            "  vigi 95     — Val-d'Oise\n"
            "  vigi 2A     — Corse-du-Sud\n"
            "Levels: Vert (✅) = no alert, Jaune (⚠️) = yellow, Orange (🟠) = orange, Rouge (🔴) = red"
        )

