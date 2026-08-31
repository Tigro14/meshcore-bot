#!/usr/bin/env python3
"""
Clock Command
Admin-only DM command that triggers a radio clock sync on the receiving bot.
Intended to be invoked remotely via the Clock_Sync_Admin scheduled job.
"""

from __future__ import annotations

from typing import Any

from ..models import MeshMessage
from .base_command import BaseCommand


class ClockCommand(BaseCommand):
    """Trigger a radio clock sync (DM only, admin only)."""

    name = "clock"
    keywords = ["clock"]
    description = "Sync the radio clock to system time (DM only, admin only)"
    requires_dm = True
    cooldown_seconds = 10
    category = "admin"

    short_description = "Sync radio clock to system time"
    usage = "clock sync admin"
    examples = ["clock sync admin"]
    parameters: list[dict[str, str]] = []

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self._clock_enabled = self.get_config_value(
            "Clock_Command",
            "enabled",
            fallback=True,
            value_type="bool",
        )

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        if not self._clock_enabled:
            return False
        return super().can_execute(message, skip_channel_check=skip_channel_check)

    def requires_admin_access(self) -> bool:
        return True

    async def execute(self, message: MeshMessage) -> bool:
        """Trigger set_radio_clock() and reply with the outcome."""
        set_radio_clock = getattr(self.bot, "set_radio_clock", None)
        if set_radio_clock is None:
            await self.send_response(message, "✗ Clock sync not available on this bot")
            return True

        try:
            ok = await set_radio_clock()
        except Exception as exc:
            self.logger.warning("Clock command: set_radio_clock raised %s", exc)
            await self.send_response(message, f"✗ Clock sync error: {exc}")
            return True

        if ok:
            await self.send_response(message, "✓ Radio clock synced")
        else:
            await self.send_response(message, "✗ Clock sync failed or not needed")
        return True
