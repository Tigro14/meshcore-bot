#!/usr/bin/env python3
"""
Airtime command for the MeshCore Bot
Reports the estimated channel airtime: the rolling window and the sender share
"""

from typing import Any

from ..models import MeshMessage
from .base_command import BaseCommand

# The module keeps three windows (fast, window, slow). A channel reply has room for one,
# and the middle one is what a reader means by "how busy is the channel right now".
_REPORT_WINDOW = 'window'

# Three names fit inside one body; more would split the reply into a second message,
# which is airtime spent on the answer itself.
_TOP_SENDERS = 3

# Names are capped so the sender list fits in one body. When even the capped list does not
# fit, the whole line is dropped and the reply says so: a name cut mid-way would read as a
# different node, and a list announced as dropped is still honest about what is missing.
_NAME_WIDTH = 12


def _render_bw(bw_khz: float) -> str:
    """Render a bandwidth without inventing precision: 125.0 reads as 125, 62.5 stays 62.5.

    Rounding every bandwidth to an integer would report a 62.5 kHz radio as 62, which is a
    different modulation and changes the airtime the reply is estimating from.
    """
    value = float(bw_khz)
    return str(int(value)) if value.is_integer() else str(value)


class AirtimeCommand(BaseCommand):
    """Reports the channel airtime estimate on demand.

    Read-only, so it is safe for scheduled ``{cmd:...}`` rendering.

    The numbers are an estimate built from the modulation (the Semtech AN1200.13
    formula), not a measurement: over serial, BLE or TCP the bot is a client of a radio
    and cannot see whether the channel was busy, only how long a packet of a given size
    occupies it. The reply says so, because a percentage on a mesh channel reads as a
    measurement.

    The two shares are reported apart and never added: rx is what this node hears, which
    is a local observation (hidden nodes, antenna gain, spatial reuse), and tx is what
    the bot spends. A single figure would present a local reading as the channel's airtime.
    """

    # Plugin metadata
    render_safe = True
    name = "airtime"
    keywords = ['airtime', 'occupancy']
    description = (
        "Show the estimated channel airtime for the last minute and the top talkers"
    )
    category = "analytics"

    # Documentation
    short_description = "Estimated channel airtime and the top talkers"
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

    def _top_senders(self, occupancy: Any) -> list[dict[str, Any]]:
        """Senders ranked by airtime, not by packet count.

        Ranking on packets would answer a different question: a talker sending many small
        packets costs more airtime than one sending a long message.
        """
        try:
            with self.bot.db_manager.connection() as conn:
                return occupancy.top_senders(conn, _TOP_SENDERS)
        except Exception as e:
            self.logger.debug(f"Airtime: could not read the sender share: {e}")
            return []

    def format_reply(self, occupancy: Any, senders: list[dict[str, Any]], limit: int) -> str:
        """Render the reply as one channel body.

        Args:
            occupancy: The bot's ChannelOccupancy instance.
            senders: Top senders as returned by ``occupancy.top_senders``.
            limit: Body budget for this message, from ``get_max_message_length``.

        Returns:
            str: The reply text, on the sender's language where the catalog has it. It
                fits within *limit* unless nothing that is honest in full can fit, in which
                case it is returned complete for the caller to split rather than cut.
        """
        mod = occupancy.modulation()
        window = occupancy.windows().get(_REPORT_WINDOW) or {}
        seconds = int(window.get('seconds') or 0)

        lines = [
            self.translate(
                'commands.airtime.header',
                sf=mod['sf'],
                bw=_render_bw(mod['bw_khz']),
                cr=mod['cr_den'],
            )
        ]

        if not window.get('samples'):
            # Nothing heard in the window is a fact about the window, not a zero percent
            # channel: a quiet channel and a bot that stopped recording are different.
            lines.append(self.translate('commands.airtime.no_samples', seconds=seconds))
        else:
            lines.append(
                self.translate(
                    'commands.airtime.window',
                    seconds=seconds,
                    rx=window['rx_percent'],
                    tx=window['tx_percent'],
                    packets=window['samples'],
                )
            )

        if not senders:
            return '\n'.join(lines)

        names = ', '.join(
            str(s.get('sender') or '?')[:_NAME_WIDTH]
            + f" {round(s.get('airtime_ms') or 0)}ms"
            for s in senders
        )
        full = '\n'.join(lines + [self.translate('commands.airtime.top_senders', names=names)])
        if len(full) <= limit:
            return full

        # The list is the only part that can be given up, and it has to be given up out
        # loud: a reply that quietly loses the talkers reads as an empty channel.
        omitted = self.translate('commands.airtime.top_senders_omitted')
        reduced = '\n'.join(lines + [omitted])
        if len(reduced) <= limit:
            return reduced

        # Nothing honest fits in one body, so the reply is returned whole and the caller
        # splits it. Splitting spends airtime on the answer, which is the lesser cost
        # compared with presenting part of the answer as the answer.
        return full

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
            reply = self.format_reply(occupancy, self._top_senders(occupancy), limit)
            if len(reply.encode('utf-8')) > limit:
                # Nothing that is honest in full fits in one body, so the reply travels as
                # two messages, split at line boundaries. The splitter keeps a name whole,
                # which a truncation at the byte limit would not.
                chunks = self.bot.command_manager.split_text_into_utf8_chunks(reply, limit)
                return await self.send_response_chunked(message, chunks)
            await self.send_response(message, reply)
            return True
        except Exception as e:
            self.logger.error(f"Airtime command error: {e}")
            await self.send_response(message, self.translate('commands.airtime.error', error=e))
            return False
