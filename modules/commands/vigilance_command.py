#!/usr/bin/env python3
"""
Vigilance Météo-France command for the MeshCore Bot
Provides current weather vigilance status for a French department
"""

import asyncio
import time

from ..models import MeshMessage
from ..service_plugins.vigilance_service import (
    _PHENOMENON_NAMES,
    _REQUEST_DELAY_SECONDS,
    fetch_vigilance,
    fetch_vigilance_multi,
    normalize_department,
    parse_departments,
)
from ..utils import get_config_timezone
from ..vigilance_alert_policy import COLORS, fingerprint
from ..vigilance_cache import VigilanceCache, publication_time
from ..vigilance_periods import calendar_days, day_label, department_label, flatten, period_key, risk_lines, split_key
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
            "description": "French department number (optional; checks all configured departments when omitted)",
        }
    ]

    # No settings_schema: the department is owned by [Vigilance_Service], so the
    # command and the monitor cannot end up configured with two different values.

    def __init__(self, bot):
        super().__init__(bot)
        self.offline_cache = self.get_config_value(
            'Vigilance_Service', 'offline_cache', fallback=False, value_type='bool',
        )
        # The command must reach its cache instead of being rejected by the
        # command manager's Internet pre-check.
        self.requires_internet = not self.offline_cache
        self.bulletin_cache = VigilanceCache(getattr(bot, 'db_manager', None), self.logger)
        self.include_tomorrow = self.get_config_value(
            'Vigilance_Service', 'include_tomorrow', fallback=False, value_type='bool',
        )

    async def execute(self, message: MeshMessage) -> bool:
        """Check the requested department, or every monitored department by default."""
        try:
            _trigger, args = self.split_trigger_and_args(message.content)
            departments = self._requested_departments(args)
            api_key = str(self.get_config_value("Vigilance_Service", "api_key", fallback="")).strip()
            delay = float(self.get_config_value(
                "Vigilance_Service", "request_delay", fallback=_REQUEST_DELAY_SECONDS,
                value_type="float",
            ))

            def fetch():
                if len(departments) == 1 and not self.include_tomorrow:
                    code = departments[0]
                    return {code: fetch_vigilance(code, api_key, delay=delay)}
                options = {'include_tomorrow': True} if self.include_tomorrow else {}
                return fetch_vigilance_multi(departments, api_key, delay=delay, **options)

            try:
                infos = await asyncio.get_running_loop().run_in_executor(None, fetch)
            except Exception as e:
                if not self.offline_cache:
                    raise
                self.logger.warning('Vigilance update unavailable: %s', type(e).__name__)
                infos = {}

            now = time.time()
            expected = ([period_key(code, day) for code in departments for day in calendar_days(now)]
                        if self.include_tomorrow else departments)
            infos = flatten(infos, self.include_tomorrow)
            current = {}
            missing = []
            for code in expected:
                data = infos.get(code)
                if (not isinstance(data, dict) or data.get('color') not in COLORS
                        or (self.offline_cache and not self.bulletin_cache.put(code, data, now))):
                    missing.append(code)
                else:
                    current[code] = data

            replies = []
            if current:
                if len(departments) == 1 and not self.include_tomorrow:
                    replies.append(self._format_response(departments[0], current[departments[0]]))
                else:
                    replies.append(self._format_current_departments(current))
            if missing:
                if self.offline_cache:
                    replies.extend(self._historical_departments(missing, now))
                else:
                    replies.append(f"Vigilance non actualisable - Depts {department_label(sorted(missing), now)}.")
            return await self.send_response(message, '\n\n'.join(replies))

        except Exception as e:
            error_msg = f"⚠️ Erreur vigilance: {str(e)}"
            await self.send_response(message, error_msg)
            return False

    def _requested_departments(self, args: str) -> list[str]:
        if args.strip():
            return [self._parse_department(args)]
        configured = str(self.get_config_value('Vigilance_Service', 'department', fallback='75'))
        return list(dict.fromkeys(parse_departments(configured))) or ['75']

    def _format_current_departments(self, infos: dict) -> str:
        """Combine identical current levels and risks without truncating other departments."""
        groups: dict[str, list[str]] = {}
        for code, info in infos.items():
            groups.setdefault(fingerprint(info), []).append(code)
        priority = {'Rouge': 0, 'Orange': 1, 'Jaune': 2, 'Vert': 3}
        replies = []
        for codes in sorted(groups.values(), key=lambda group: priority[infos[group[0]]['color']]):
            data = infos[codes[0]]
            color = data['color']
            emoji = {'Vert': '✅', 'Jaune': '⚠️', 'Orange': '🟠', 'Rouge': '🔴'}[color]
            department_codes = '/'.join(sorted(split_key(code)[0] for code in codes))
            period = ' - ' + day_label(data['period_date'], time.time()) if data.get('period_date') else ''
            lines = [f"{emoji} Vigilance {department_codes}{period} : {color}"]
            lines.extend(risk_lines(data, _PHENOMENON_NAMES))
            replies.append('\n'.join(lines))
        return '\n'.join(replies)

    def _historical_departments(self, codes: list[str], now: float) -> list[str]:
        """Keep historical and current information separate, including their dates."""
        groups: dict[tuple, list[str]] = {}
        for code in codes:
            record = self.bulletin_cache.get(code)
            key = ('missing', split_key(code)[1]) if record is None else (
                self.bulletin_cache.status(code, now), fingerprint(record['info']),
                publication_time(record['info']), record['info'].get('valid_until'),
            )
            groups.setdefault(key, []).append(code)
        tz = get_config_timezone(self.bot.config, self.logger)[0]
        return [self.bulletin_cache.historical_reply(
            group[0], now, tz, phenomenon_names=_PHENOMENON_NAMES,
            department_label=f"{'Depts' if len(group) > 1 else 'Dept'} {'/'.join(sorted(split_key(code)[0] for code in group))}",
        ) for group in groups.values()]

    def _parse_department(self, args: str) -> str:
        """Parse department code from the arguments after the trigger keyword.

        Accepts the numeric codes (``95``) and the Corsican ones (``2a``/``2b``),
        which Météo-France writes as ``2A``/``2B``. Falls back to the configured
        department, then '75'. This single-department parser also handles invalid
        explicit arguments; the default multi-department query uses _requested_departments.

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
            "  vigi        — checks all configured departments\n"
            "  vigi 75     — Paris\n"
            "  vigi 95     — Val-d'Oise\n"
            "  vigi 2A     — Corse-du-Sud\n"
            "Levels: Vert (✅) = no alert, Jaune (⚠️) = yellow, Orange (🟠) = orange, Rouge (🔴) = red"
        )

