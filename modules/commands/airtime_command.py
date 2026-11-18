#!/usr/bin/env python3
"""
Airtime command for the MeshCore Bot
Answers the channel occupancy question in one body: the day's peak and the
average share over the last hour and over the day.
"""

import time
from typing import Any, Optional

from ..models import MeshMessage
from .base_command import BaseCommand

# One hour, in the milliseconds the buckets' rx_ms column is priced in.
_HOUR_MS = 3_600_000


def _render_g(value: Any) -> str:
    """Render a computed percentage without false precision: 12.0 reads as 12, 2.40 stays 2.4.

    One decimal is what the reading holds; a second one would claim a precision
    the bucket sums do not carry, in a reply budgeted to fit in a single body.
    """
    return f"{float(value):g}"


class AirtimeCommand(BaseCommand):
    """Reports the modelled channel occupancy on demand: peak today, averages now.

    Read-only, so it is safe for scheduled ``{cmd:...}`` rendering.

    The three figures answer "how busy is the channel" and nothing else — the
    network view, the per-station ledgers and the modulation behind the estimate
    live on the /airtime page, where there is room for them.

    All three are the rx share: what this radio hears. The bot cannot merge in
    what it does not hear — hidden nodes and other radios' readings are not this
    process's facts — so the reply is this antenna's view of the channel, and it
    says "estimée/modelled" because the figure is computed from the modulation
    (the Semtech AN1200.13 formula), not measured off the carrier.

    The day is the UTC day the buckets are aligned on, the same alignment as the
    day tables; the averages divide by the capacity actually elapsed, not by the
    buckets that happen to exist, so silence prices as silence rather than
    flattering itself by counting only busy minutes. A share above 100 % is
    possible and is not clamped: parallel transmissions each cost their airtime.
    With no sample recorded today the reply says there is nothing recorded,
    rather than answering 0 % for a period it never saw.
    """

    # Plugin metadata
    render_safe = True
    name = "airtime"
    keywords = ['airtime', 'occupancy']
    description = (
        "Show the modelled channel occupancy for today (UTC): the day's peak, and the"
        " average share over the last hour and over the day"
    )
    category = "analytics"

    # Documentation
    short_description = "Modelled channel occupancy: day peak, last-hour and day averages"
    usage = "airtime"
    examples = ["airtime"]

    def __init__(self, bot: Any):
        """Initialize the airtime command.

        Args:
            bot: The bot instance.
        """
        super().__init__(bot)
        self.command_enabled = self.get_config_value(
            'Airtime_Command', 'enabled', fallback=True, value_type='bool'
        )

    def get_help_text(self, message: MeshMessage | None = None) -> str:
        """Get help text for the airtime command.

        Returns:
            str: The help text for this command.
        """
        return self.translate('commands.airtime.description')

    def _day_stats(self, occupancy: Any) -> Optional[dict[str, float]]:
        """Peak and averages over the stored buckets of the current UTC day.

        One read, three figures. The peak is the worst bucket of the day at the
        bucket's own capacity; the averages divide the summed rx_ms by the
        capacity elapsed — the whole day so far, and the whole last hour — so
        quiet minutes count as quiet rather than dropping out of the denominator.

        Returns None when the day holds no bucket. An absent day is an absent fact:
        it leaves the numbers out, it does not report a calm the recorder never
        witnessed. A failed read is a different absent fact — it is raised, so the
        caller can say 'read error' rather than 'nothing recorded today': the two
        read the same on a quiet channel and mean opposite things.
        """
        now = time.time()
        day_start = int(now // 86400) * 86400
        with self.bot.db_manager.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*), SUM(rx_ms), MAX(rx_ms), "
                "SUM(CASE WHEN bucket_ts >= ? THEN rx_ms ELSE 0 END) "
                "FROM occupancy_samples WHERE sample_seconds = ? AND bucket_ts >= ?",
                (int(now - _HOUR_MS / 1000), int(occupancy.sample_seconds), day_start),
            ).fetchone()
        if row is None or not row[0]:
            return None
        _, day_sum, peak_ms, hour_sum = row
        return {
            'peak_percent': float(peak_ms or 0) / (occupancy.sample_seconds * 1000) * 100,
            'hour_percent': float(hour_sum or 0) / _HOUR_MS * 100,
            'day_percent': float(day_sum or 0) / ((now - day_start) * 1000) * 100,
        }

    def format_reply(self, stats: Optional[dict[str, float]], limit: int) -> str:
        """Render the reply as one channel body.

        Args:
            stats: The figures from ``_day_stats``; None renders the nothing-
                recorded line rather than a invented zero.
            limit: Body budget for this message, from ``get_max_message_length``.

        Returns:
            str: The reply text, on the sender's language where the catalog has it.
        """
        if stats is None:
            return self.translate('commands.airtime.no_samples')
        return self.translate(
            'commands.airtime.reply',
            peak=_render_g(round(stats['peak_percent'], 1)),
            hour=_render_g(round(stats['hour_percent'], 1)),
            day=_render_g(round(stats['day_percent'], 1)),
        )

    async def execute(self, message: MeshMessage) -> bool:
        """Execute the airtime command.

        Args:
            message: The message triggering the command.

        Returns:
            bool: True if executed successfully, False otherwise.
        """
        if not self.command_enabled:
            await self.send_response(message, self.translate('commands.airtime.disabled'))
            return False

        occupancy = getattr(self.bot, 'occupancy', None)
        if occupancy is None:
            await self.send_response(message, self.translate('commands.airtime.unavailable'))
            return False

        if not occupancy.enabled:
            # Off by default, so the reply has to say why there is nothing to read rather
            # than answer 0%.
            await self.send_response(message, self.translate('commands.airtime.recording_off'))
            return False

        try:
            limit = self.get_max_message_length(message)
            reply = self.format_reply(self._day_stats(occupancy), limit)
            if len(reply.encode('utf-8')) > limit:
                # Kept for the same reason every reply keeps it: a cramped body
                # (a long sender name in a regional channel) still gets the whole
                # answer in pieces rather than half of one, through the splitter
                # every long reply uses.
                chunks = self.bot.command_manager.split_text_into_utf8_chunks(reply, limit)
                return await self.send_response_chunked(message, chunks)
            await self.send_response(message, reply)
            return True
        except Exception as e:
            self.logger.error(f"Airtime command error: {e}")
            await self.send_response(message, self.translate('commands.airtime.error', error=e))
            return False
