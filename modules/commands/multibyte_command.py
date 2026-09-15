#!/usr/bin/env python3
"""
Multibyte Command - lists local repeaters still using 1-byte routing.

Broadcastable: shows the top N (default 5) currently-tracked repeaters/room
servers classified as 1-byte only, most recently seen first. Uses the same
evidence as the web viewer ``/contacts`` path-encoding badge, so the list
always agrees with the badge shown in the viewer.
"""

from datetime import datetime
from typing import Any

from ..models import MeshMessage
from ..multibyte_detection import count_tracked_repeaters, find_onebyte_repeaters
from .base_command import BaseCommand


class MultibyteCommand(BaseCommand):
    """List currently-tracked local repeaters still on 1-byte routing.

    A repeater/roomserver counts as 1-byte when, like the ``/contacts`` badge,
    it has path evidence and no multibyte signal (no 2/3-byte out-encoding,
    no multibyte observed paths, and its public-key prefix is not a hop on a
    multibyte path).
    """

    # Plugin metadata
    name = "multibyte"
    keywords = ["multibyte"]
    description = "List local repeaters still using 1-byte routing"
    category = "network"
    # Read-only; leave False until the team confirms it is safe in scheduled
    # messages (it would leak the 1-byte rollout state to any scheduler).
    render_safe = False

    # Documentation
    short_description = "List local repeaters still using 1-byte routing"
    usage = "multibyte [N]"
    examples = ["multibyte", "multibyte 10"]
    parameters = [{"name": "N", "description": "Max number of repeaters to list (default 5)"}]

    DEFAULT_TOP = 5
    MAX_TOP = 25

    def __init__(self, bot: Any):
        super().__init__(bot)
        self.multibyte_enabled = self.get_config_value("Multibyte_Command", "enabled", fallback=True, value_type="bool")

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        """Check if this command can be executed with the given message."""
        if not self.multibyte_enabled:
            return False
        return super().can_execute(message, skip_channel_check=skip_channel_check)

    def _parse_top(self, args: list[str]) -> int:
        """First numeric arg as the result cap, else the default."""
        for a in args:
            if a.isdigit():
                return max(1, min(int(a), self.MAX_TOP))
        return self.DEFAULT_TOP

    @staticmethod
    def _format_seen(last_heard: Any) -> str:
        """Compact 'seen' age for an ISO datetime string."""
        if not last_heard:
            return "?"
        try:
            dt = datetime.fromisoformat(str(last_heard))
        except (TypeError, ValueError):
            return str(last_heard)[:8]
        secs = max(0, int((datetime.now() - dt).total_seconds()))
        if secs < 60:
            return f"{secs}s"
        mins = secs // 60
        if mins < 60:
            return f"{mins}m"
        hours = mins // 60
        if hours < 24:
            return f"{hours}h"
        return f"{hours // 24}d"

    def _format_response(
        self,
        results: list[dict[str, Any]],
        total: int,
        top: int,
        max_len: int,
    ) -> str:
        """Render the list (or the all-multibyte case) within ``max_len`` UTF-8 bytes.

        The fixed description ("1-byte local repeaters, most recent first") lives
        in the help text; the on-air reply keeps only the count and as many lines
        as fit in the RF budget.
        """
        if not results:
            return f"All {total} tracked repeaters multibyte ✓"

        lines = [f"1B {len(results)} of {total}:"]
        for i, r in enumerate(results[:top], 1):
            name = (r.get("name") or "Unknown")[:24]
            adv = r.get("advert_count") or 0
            seen = self._format_seen(r.get("last_heard"))
            line = f"{i}. {name} · {adv} adv · {seen}"
            candidate = "\n".join(lines + [line])
            if len(candidate.encode("utf-8")) > max_len:
                break
            lines.append(line)
        return "\n".join(lines)

    async def execute(self, message: MeshMessage) -> bool:
        """Execute the multibyte command."""
        parts = message.content.strip().split()
        top = self._parse_top(parts[1:])

        db_manager = getattr(self.bot, "db_manager", None)
        if db_manager is None:
            await self.send_response(message, "❌ Database not available.")
            return True

        try:
            with db_manager.connection() as conn:
                results = find_onebyte_repeaters(conn, top_n=top, logger=self.logger)
                total = count_tracked_repeaters(conn)
        except Exception as e:
            self.logger.error(f"Multibyte command error: {e}")
            await self.send_response(message, f"❌ Error: {e}")
            return True

        max_len = self.get_max_message_length(message)
        await self.send_response(message, self._format_response(results, total, top, max_len))
        return True

    def get_help_text(self) -> str:
        """Get help text for the multibyte command."""
        return (
            f"multibyte [N] — 1-byte local repeaters, most recent first (N max, default {self.DEFAULT_TOP}). "
            f"Reply: '1B n of t:' + 'name · adv · seen'; 'All ... multibyte' = none left"
        )
