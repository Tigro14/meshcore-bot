#!/usr/bin/env python3
"""
Airtime command for the MeshCore Bot
Reports the estimated channel airtime: the rolling window, the local network view,
and the sender share
"""

import math
import time
from typing import Any, Optional

from ..models import MeshMessage
from .base_command import BaseCommand

# The module keeps three windows (fast, window, slow). A channel reply has room for one,
# and the middle one is what a reader means by "how busy is the channel right now".
_REPORT_WINDOW = 'window'

# The sender share is this radio's own transmissions over a rolling week. The stored
# buckets are kept for a month by default, so the week fits inside the retention; when
# it does not — a fresh install, a shorter retention setting — the reply's /Nd suffix
# says what period was actually covered rather than claiming seven days.
_RADIO_DAYS = 7

# Names are capped so the reply fits in one body. When even the capped line does not
# fit, the whole line is dropped and the reply says so: a name cut mid-way would read as
# a different node, and a line announced as dropped is still honest about what is missing.
_NAME_WIDTH = 12

# Density recency, matching how the repeater manager counts nodes heard recently
# (last_heard/last_advert within a day). Adverts are hours apart, so an hour window
# would read a known node that merely has not advertised yet as absent. Not
# configurable: like the other airtime guards, it is a deliberate constant, and the
# reply is one body whether the value is 24 or anything else.
_STATIONS_HOURS = 24


def _render_bw(bw_khz: float) -> str:
    """Render a bandwidth without inventing precision: 125.0 reads as 125, 62.5 stays 62.5.

    Rounding every bandwidth to an integer would report a 62.5 kHz radio as 62, which is a
    different modulation and changes the airtime the reply is estimating from.
    """
    value = float(bw_khz)
    return str(int(value)) if value.is_integer() else str(value)


def _render_g(value: Any) -> str:
    """Render a computed count without false precision: 1.0 reads as 1, 2.40 stays 2.4.

    The repetition factor and the average hop depth are window averages; writing 2.0
    claims a decimal place the reading does not have, and one byte is one byte of
    airtime in a reply that is already budgeted to fit in a single body.
    """
    return f"{float(value):g}"


def _render_duration(airtime_ms: float) -> str:
    """Render a duration in the unit its size deserves: 900 ms, 12.5 s, 17.5 min, 4.2 h.

    A cumulative week of airtime in milliseconds (1052937ms) is the number nobody can
    read at a glance on a channel screen; the magnitude is rounded to one decimal and
    whole values lose the decimal entirely.
    """
    ms = float(airtime_ms)
    if ms < 1000:
        return _render_g(round(ms)) + " ms"
    if ms < 60_000:
        return _render_g(round(ms / 1000, 1)) + " s"
    if ms < 3_600_000:
        return _render_g(round(ms / 60_000, 1)) + " min"
    return _render_g(round(ms / 3_600_000, 1)) + " h"


class AirtimeCommand(BaseCommand):
    """Reports the channel airtime estimate and the local network view on demand.

    Read-only, so it is safe for scheduled ``{cmd:...}`` rendering.

    The numbers are an estimate built from the modulation (the Semtech AN1200.13
    formula), not a measurement: over serial, BLE or TCP the bot is a client of a radio
    and cannot see whether the channel was busy, only how long a packet of a given size
    occupies it. The reply says so, because a percentage on a mesh channel reads as a
    measurement.

    The two shares are reported apart and never added: rx is what this node hears, which
    is a local observation (hidden nodes, antenna gain, spatial reuse), and tx is what
    the bot spends. A single figure would present a local reading as the channel's airtime.

    The network line reports what the node heard, not who was on air: the talkers
    this radio can name and, beside them, the copies it heard without an identity —
    the two reconcile the packet count — the emitter addresses the flood path proved
    for this window (labels, not claimed stations), the repetition factor the flood
    router leaves behind (a floor, since hashes cover only what this node heard),
    the average hop depth of heard traffic, and the count of stations heard recently
    from the contact tracking already running for the contacts page. The whole
    network line is carried only by the window's samples: with nothing heard there
    is nothing to count, and the no-samples line already says that.

    The sender line carries this radio's own transmitted airtime over a rolling week,
    named and suffixed /7d. It names only this radio because it is the only sender the
    bot has complete knowledge of: what this node hears is partly attributable — an
    advert names its advertiser, and the /airtime page lists every sender it can name,
    with an unattributed bucket for the rest — but a message packet carries its route,
    not its sender, so a per-sender view squeezed into one channel body would read as
    the whole channel while naming only part of it. The period suffix is the
    retention's truth too: when the stored buckets cover less than the week, the reply
    says how much they cover.
    """

    # Plugin metadata
    render_safe = True
    name = "airtime"
    keywords = ['airtime', 'occupancy']
    description = (
        "Show the estimated channel airtime for the last minute, the local network view,"
        " and this radio's own airtime share for the past week"
    )
    category = "analytics"

    # Documentation
    short_description = (
        "Estimated channel airtime, the local network view and this radio's airtime share"
    )
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

    def _radio_name(self) -> str:
        """Display name for this radio: advertised name, contact record, key prefix.

        The prefixes are tried in that order because they descend in certainty. A
        contact row for the radio's own full public key is the same identity; a key
        prefix is never resolved through the contacts table, because a 1-2 byte prefix
        names a whole crowd of strangers as readily as it names this radio, and a wrong
        name is worse than an opaque one.
        """
        meshcore = getattr(self.bot, "meshcore", None)
        self_info = getattr(meshcore, "self_info", None) if meshcore else None
        if isinstance(self_info, dict):
            name = self_info.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()[:_NAME_WIDTH]
            key = self_info.get("public_key")
            if isinstance(key, str) and key:
                try:
                    with self.bot.db_manager.connection() as conn:
                        row = conn.execute(
                            "SELECT name FROM complete_contact_tracking WHERE public_key = ? LIMIT 1",
                            (key,),
                        ).fetchone()
                    if row is not None and row[0]:
                        return str(row[0]).strip()[:_NAME_WIDTH]
                except Exception as e:
                    self.logger.debug(f"Airtime: could not resolve the radio name: {e}")
        prefix = getattr(getattr(self.bot, "transmission_tracker", None), "bot_prefix", None)
        return str(prefix or "bot")[:_NAME_WIDTH]

    def _bot_radio_share(self, occupancy: Any) -> Optional[dict[str, Any]]:
        """This radio's transmitted airtime over the rolling look-back, or None if unread.

        Summed from the stored buckets rather than the cumulative sender table: the
        buckets carry their own timestamp, so a rolling period is a WHERE clause, and
        the oldest bucket read tells whether the period was really covered. Only tx is
        summed — that is the airtime this radio put on the channel; what it heard
        belongs to senders this node cannot identify (see the class docstring).

        A zero is a reported fact here, unlike the unknown network counts: the bot
        knows every packet it sent, so "nothing for a week" is an observation, not an
        absence of evidence.
        """
        now = time.time()
        cutoff = now - _RADIO_DAYS * 86400
        try:
            with self.bot.db_manager.connection() as conn:
                row = conn.execute(
                    "SELECT SUM(tx_ms), MIN(bucket_ts) FROM occupancy_samples "
                    "WHERE sample_seconds = ? AND bucket_ts >= ?",
                    (int(occupancy.sample_seconds), int(cutoff)),
                ).fetchone()
            if row is None or row[0] is None:
                return None
            first_ts = float(row[1] or now)
            # Rounding to three decimals before the ceiling absorbs clock skew between
            # this reader and the writer (bucket_ts is another clock's reading), so an
            # exactly-two-days-old bucket does not promote itself into a claimed
            # three-day period. Three decimals of a day is about 86 s, which covers
            # the drift between two NTP-disciplined hosts and is still far below any
            # period worth arguing about in the reply.
            days = max(1, min(_RADIO_DAYS, math.ceil(round((now - first_ts) / 86400.0, 3))))
            return {"airtime_ms": float(row[0]), "period_days": days}
        except Exception as e:
            # A failed read — or a row that does not have the two columns the read
            # asked for — is a missing fact, and a missing fact leaves the line out
            # rather than reporting a number it cannot support.
            self.logger.debug(f"Airtime: could not read the radio's airtime share: {e}")
            return None

    def _heard_stations(self) -> Optional[int]:
        """Stations heard within the recency window, from the contact tracking.

        Advert-based presence, so the count answers "how many stations did this node
        see exist", not "how many were talking" — the talkers figure in the network
        line is the other question. None (not 0) when the read fails or the table is
        not there: an absent clause is a missing fact, a 0 would claim an empty area.
        """
        try:
            with self.bot.db_manager.connection() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM complete_contact_tracking "
                    "WHERE COALESCE(last_advert_timestamp, last_heard) >= datetime('now', ?)",
                    (f'-{_STATIONS_HOURS} hours',),
                ).fetchone()
            return int(row[0]) if row is not None else None
        except Exception as e:
            self.logger.debug(f"Airtime: could not read the station count: {e}")
            return None

    def _count_clause(self, entry: str, n: int) -> str:
        """One count clause, agreeing with its own number.

        The plural form lives under *entry*, the singular under ``{entry}_one``.
        A language whose catalogue carries no singular keeps the plural: the
        translator answers an unknown key with the key itself, and printing
        ``commands.airtime.network_talkers_one`` in a mesh reply would cost more
        airtime than a telegraphic agreement does.
        """
        key = f'commands.airtime.{entry}'
        if n == 1:
            singular = self.translate(f'{key}_one')
            if singular != f'{key}_one':
                return singular.format(n=n)
        return self.translate(key).format(n=n)

    def _network_line(self, window: dict[str, Any], stations: Optional[int]) -> tuple[Optional[str], Optional[str]]:
        """Build the network line as (with stations, without stations), either None.

        Two variants because the stations clause is the first thing given up when the
        body runs out of room: it is the least specific number in the line, and both
        variants stay truthful about what they do count. Fragments are only joined for
        counts the window actually holds — an unknown repetition factor is left out
        rather than rendered as x1, which would read as "no repeats heard".
        """
        parts: list[str] = []
        talkers = window.get('distinct_senders') or 0
        if talkers:
            parts.append(self._count_clause('network_talkers', talkers))
        unattributed = window.get('unattributed_packets') or 0
        if unattributed:
            # The copies the window heard but could not name, stated next to the
            # talkers count: 2 packets and 1 talker reconcile only when the
            # reader can see where the second packet went.
            parts.append(self._count_clause('network_unattributed', unattributed))
        addrs = window.get('emitter_labels') or 0
        if addrs:
            # Emitter labels are a different count from talkers: the addresses the
            # flood path proved carried these copies, prefixes included. "Who
            # occupied the channel in this window" is this number's question, not
            # the talkers'.
            parts.append(self._count_clause('network_emitters', addrs))
        dup = window.get('dup_factor')
        if dup:
            parts.append(self.translate('commands.airtime.network_repeats', dup=_render_g(dup)))
        avg_hops = window.get('avg_hops')
        if avg_hops is not None:
            parts.append(self.translate('commands.airtime.network_hops', hops=_render_g(avg_hops)))
        if not parts:
            return None, None

        prefix = self.translate('commands.airtime.network_prefix')
        base = f"{prefix} {', '.join(parts)}"
        if stations:
            stations_clause = self.translate(
                'commands.airtime.network_stations', stations=stations, hours=_STATIONS_HOURS
            )
            return f"{base}, {stations_clause}", base
        return base, None

    def format_reply(
        self,
        occupancy: Any,
        radio_share: Optional[dict[str, Any]],
        limit: int,
        stations: Optional[int] = None,
    ) -> str:
        """Render the reply as one channel body.

        Args:
            occupancy: The bot's ChannelOccupancy instance.
            radio_share: This radio's airtime share as returned by ``_bot_radio_share``;
                None leaves the line out.
            limit: Body budget for this message, from ``get_max_message_length``.
            stations: Stations heard recently, from ``_heard_stations``; None leaves
                the clause out.

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
            # And with no samples there is no network to count either.
            lines.append(self.translate('commands.airtime.no_samples', seconds=seconds))
            network_line = None
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
            network_line = self._network_line(window, stations)

        core = '\n'.join(lines)
        network_with, network_base = network_line if network_line else (None, None)

        # Candidates in cost order: everything, then the stations clause given up, and
        # the radio share line given up out loud only when there is no network line
        # competing for the room — a reply that quietly loses the sender reads as a
        # silent radio, but the stations clause costs more than a note about it would.
        tails: list[Optional[str]] = []
        if radio_share:
            entry = (
                f"{self._radio_name()} {_render_duration(radio_share.get('airtime_ms') or 0)}"
                f"/{int(radio_share.get('period_days') or _RADIO_DAYS)}d"
            )
            tails.append(self.translate('commands.airtime.radio_share', entry=entry))

        if network_with or network_base:
            candidates = [
                '\n'.join(part for part in (core, network, tail) if part)
                for network in (network_with, network_base)
                for tail in (tails or (None,))
                if network
            ]
        else:
            # No network line to carry: the radio share line can then be given up with
            # its note, and core alone is the last candidate.
            if radio_share:
                tails.append(self.translate('commands.airtime.top_senders_omitted'))
            candidates = ['\n'.join(part for part in (core, tail) if part) for tail in tails] or [core]

        for text in candidates:
            # The channel's body budget is bytes, not characters: an accented reply
            # that fits in characters can still overflow the wire and be split by the
            # radio itself, mid-word.  The ladder compares the same currency it is
            # spending.
            if len(text.encode('utf-8')) <= limit:
                return text

        # Nothing fits in one body. The reply is returned whole — the network line is
        # part of the answer, not an extra, so it is never given up silently — and the
        # caller splits it. Splitting spends airtime on the answer, which is the lesser
        # cost compared with presenting part of the answer as the answer.
        whole = '\n'.join(part for part in (core, network_with or network_base, tails[0] if tails else None) if part)
        return whole

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
            reply = self.format_reply(occupancy, self._bot_radio_share(occupancy), limit, self._heard_stations())
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
