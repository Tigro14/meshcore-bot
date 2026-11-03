#!/usr/bin/env python3
"""
Vigilance Météo Service for MeshCore Bot

Polls Météo-France vigilance weather alerts for a French department and posts
to a configured channel when the alert level reaches Orange or Rouge.

The scraper directly fetches https://vigilance.meteofrance.fr — no external
package needed (the old 'vigilancemeteo' package is unmaintained).
"""

import asyncio
import contextlib
import http.client
import random
import re
import socket
import time
from datetime import datetime
from typing import Any, Optional

import requests
from bs4 import BeautifulSoup, Tag

from .base_service import BaseServicePlugin

# Default département → URL-slug mapping for Météo-France vigilance pages.
_DEPT_URL_NAMES: dict[str, str] = {
    '06': 'nice', '13': 'marseille', '14': 'caen', '21': 'dijon',
    '25': 'besancon', '29': 'brest', '31': 'toulouse', '33': 'bordeaux',
    '34': 'montpellier', '35': 'rennes', '37': 'tours', '44': 'nantes',
    '45': 'orleans', '54': 'nancy', '57': 'metz', '59': 'lille',
    '63': 'clermont-ferrand', '67': 'strasbourg', '69': 'lyon',
    '75': 'paris', '76': 'rouen', '80': 'amiens', '86': 'poitiers',
    '87': 'limoges',
}

_COLOR_CLASSES = ['vert', 'jaune', 'orange', 'rouge', 'green', 'yellow', 'red']
_SKIP_PATTERNS = ['legend', 'legende', 'footer', 'nav', 'menu', 'list', 'liste']
_ALERT_INDICATORS = ['alert', 'alerte', 'status', 'niveau', 'vigilance-level', 'current']

_VIGILANCE_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7',
}


def _normalize_color(color: str) -> str:
    """Return a canonical vigilance color string."""
    c = color.lower()
    if c in ('vert', 'green'):
        return 'Vert'
    if c in ('jaune', 'yellow'):
        return 'Jaune'
    if c == 'orange':
        return 'Orange'
    if c in ('rouge', 'red'):
        return 'Rouge'
    return 'Vert'


def _attr(el: Tag, name: str) -> str:
    """Read an attribute as a plain string.

    bs4 returns a list for class attributes (and can return None), so joining
    through ``str`` here keeps the callers from having to narrow the type.
    """
    value = el.get(name)
    if isinstance(value, (list, tuple)):
        return ' '.join(str(part) for part in value)
    return str(value or '')


def _extract_color(soup: BeautifulSoup) -> str:
    """Extract the current vigilance color level from a Météo-France page."""
    # 1. data-color / data-level attributes (most reliable)
    for el in soup.find_all(lambda tag: tag.has_attr('data-color')):
        c = _attr(el, 'data-color').lower()
        if c:
            return _normalize_color(c)
    for el in soup.find_all(lambda tag: tag.has_attr('data-level')):
        c = _attr(el, 'data-level').lower()
        if c:
            return _normalize_color(c)

    # 2. Alert-indicator CSS classes
    for el in soup.find_all(class_=True):
        classes = _attr(el, 'class').lower()
        if any(skip in classes for skip in _SKIP_PATTERNS):
            continue
        if any(ind in classes for ind in _ALERT_INDICATORS):
            for color in _COLOR_CLASSES:
                if color in classes:
                    return _normalize_color(color)

    # 3. Compound class prefixes like "vigilance-rouge" or "alerte-orange"
    for el in soup.find_all(class_=True):
        classes = _attr(el, 'class').lower()
        parent = el.find_parent()
        if parent:
            pcls = _attr(parent, 'class').lower() if parent.get('class') else ''
            if any(skip in pcls for skip in _SKIP_PATTERNS):
                continue
        for prefix in ('vigilance-', 'alerte-', 'niveau-', 'alert-', 'status-'):
            for color in _COLOR_CLASSES:
                if f'{prefix}{color}' in classes:
                    return _normalize_color(color)

    # 4. Meta tags
    for meta in soup.find_all('meta', attrs={'name': re.compile(r'vigilance|couleur|niveau', re.I)}):
        content = _attr(meta, 'content').lower()
        for color in ('rouge', 'orange', 'jaune', 'vert'):
            if color in content:
                return _normalize_color(color)

    # 5. Active-alert text patterns
    text = soup.get_text().lower()
    active_suffix = r'(en\s+cours|activ[ée]|pour\s+le|jusqu)'
    for prefix in ('vigilance', 'alerte', 'niveau'):
        for color in ('rouge', 'orange', 'jaune'):
            if re.search(rf'{prefix}\s+{color}\s+{active_suffix}', text):
                return _normalize_color(color)

    # 6. Explicit no-alert patterns → Vert
    for pattern in (
        r'pas\s+de\s+vigilance\s+particulière',
        r'aucune\s+vigilance',
        r'vigilance\s+verte',
        r'niveau\s+vert',
    ):
        if re.search(pattern, text):
            return 'Vert'

    return 'Vert'


def _extract_summary(soup: BeautifulSoup, color: str) -> str:
    """Extract a human-readable summary from the page."""
    for selector in ('summary', 'resume', 'alerte', 'message', 'description'):
        for el in (soup.find_all(class_=re.compile(selector, re.I)) or
                   soup.find_all(id=re.compile(selector, re.I))):
            text = el.get_text(strip=True)
            if len(text) > 20:
                return text

    for p in soup.find_all('p'):
        text = p.get_text(strip=True)
        if any(kw in text.lower() for kw in ('vigilance', 'alerte', 'phénomène', 'météo')):
            if len(text) > 20:
                return text

    for lst in soup.find_all(('ul', 'ol')):
        items = lst.find_all('li')
        if items and any(kw in lst.get_text().lower() for kw in ('vigilance', 'alerte', 'phénomène')):
            parts = [f"- {item.get_text(strip=True)}" for item in items[:3] if item.get_text(strip=True)]
            if parts:
                return '\n'.join(parts)

    if color == 'Vert':
        return "Pas de vigilance particulière"
    return f"Vigilance {color} en cours. Consultez vigilance.meteofrance.fr pour plus de détails."


def _extract_bulletin_date(soup: BeautifulSoup) -> datetime:
    """Extract the bulletin date from the page, falling back to now()."""
    date_formats = (
        '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d',
        '%d/%m/%Y %H:%M:%S', '%d/%m/%Y %H:%M', '%d/%m/%Y', '%d-%m-%Y',
    )

    def _try_parse(s: str) -> Optional[datetime]:
        if not s:
            return None
        for fmt in date_formats:
            try:
                return datetime.strptime(s.strip(), fmt)
            except (ValueError, AttributeError):
                continue
        m = re.search(r'\d{4}-\d{2}-\d{2}', s)
        if m:
            try:
                return datetime.strptime(m.group(), '%Y-%m-%d')
            except ValueError:
                pass
        return None

    for selector in ('date', 'bulletin', 'mise-a-jour', 'update', 'timestamp'):
        for el in (soup.find_all(class_=re.compile(selector, re.I)) or
                   soup.find_all(id=re.compile(selector, re.I))):
            d = _try_parse(el.get_text(strip=True))
            if d:
                return d

    for tel in soup.find_all('time'):
        d = _try_parse(_attr(tel, 'datetime')) or _try_parse(tel.get_text(strip=True))
        if d:
            return d

    for meta in soup.find_all('meta', attrs={'property': re.compile(r'date|time', re.I)}):
        d = _try_parse(_attr(meta, 'content'))
        if d:
            return d

    return datetime.now()


def fetch_vigilance(department: str, timeout: int = 10) -> dict[str, Any]:
    """
    Fetch current vigilance data for a French department from Météo-France.

    Returns a dict with keys: color, summary, bulletin_date, url.
    Raises requests.exceptions.RequestException on network errors.
    """
    slug = _DEPT_URL_NAMES.get(department, department)
    url = f"https://vigilance.meteofrance.fr/fr/{slug}"
    response = requests.get(url, headers=_VIGILANCE_HEADERS, timeout=timeout)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, 'html.parser')
    color = _extract_color(soup)
    return {
        'color': color,
        'summary': _extract_summary(soup, color),
        'bulletin_date': _extract_bulletin_date(soup),
        'url': url,
    }


class VigilanceService(BaseServicePlugin):
    """
    Polls Météo-France vigilance alerts for a French department and posts to a
    channel when Orange or Rouge vigilance is active.

    Config section: ``[Vigilance_Service]``
    """

    config_section = 'Vigilance_Service'
    description = "Météo-France weather vigilance alert monitor (France)"

    settings_schema = [
        {
            "key": "department",
            "label": "Department number",
            "type": "str",
            "default": "75",
            "help": "French department number to monitor (e.g. 75 for Paris, 25 for Doubs).",
            "required": True,
        },
        {
            "key": "channel",
            "label": "Alert channel",
            "type": "str",
            "default": "#general",
            "help": "Channel to post vigilance alerts to.",
        },
        {
            "key": "poll_interval",
            "label": "Poll interval",
            "type": "int",
            "min": 60000,
            "default": 28800000,
            "unit": "ms",
            "help": "How often to check Météo-France (milliseconds). Default 8 h.",
        },
        {
            "key": "alert_throttle",
            "label": "Alert throttle",
            "type": "int",
            "min": 60,
            "default": 3600,
            "unit": "s",
            "help": "Minimum seconds between successive alerts for the same event.",
        },
        {
            "key": "alert_levels",
            "label": "Alert levels",
            "type": "str",
            "default": "Orange,Rouge",
            "help": "Comma-separated vigilance levels that trigger an alert (e.g. Orange,Rouge).",
        },
        {
            "key": "flood_scope",
            "label": "Flood scope",
            "type": "str",
            "default": "",
            "help": "Optional regional TC_FLOOD scope for mesh posts (e.g. #west).",
        },
        {
            "key": "silence_mesh_output",
            "label": "Silence mesh output",
            "type": "bool",
            "default": False,
            "help": "When true, alerts go only to Discord/Telegram webhooks, not the mesh channel.",
        },
    ]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        section = self.config_section

        self.department: str = self.bot.config.get(section, 'department', fallback='75').strip()
        self.channel: str = self.bot.config.get(section, 'channel', fallback='#general').strip()
        poll_ms = self.bot.config.getint(section, 'poll_interval', fallback=28800000)
        self.poll_interval_seconds: float = poll_ms / 1000.0
        self.alert_throttle: int = self.bot.config.getint(section, 'alert_throttle', fallback=3600)
        raw_levels = self.bot.config.get(section, 'alert_levels', fallback='Orange,Rouge')
        self.alert_levels: list[str] = [lvl.strip() for lvl in raw_levels.split(',') if lvl.strip()]
        self.silence_mesh: bool = self.bot.config.getboolean(
            section, 'silence_mesh_output', fallback=False
        )

        # Runtime state
        self._running = False
        self._poll_task: Optional[asyncio.Task] = None
        self._last_color: Optional[str] = None
        self._last_bulletin_date: Optional[datetime] = None
        self._last_alert_time: float = 0.0

        self.logger.info(
            "Vigilance service initialized: dept=%s channel=%s interval=%.0fs throttle=%ds levels=%s",
            self.department, self.channel, self.poll_interval_seconds,
            self.alert_throttle, ', '.join(self.alert_levels),
        )

    async def start(self) -> None:
        if not self.enabled:
            self.logger.info("Vigilance service is disabled, not starting")
            return
        self._running = True
        self.logger.info("Starting vigilance service for department %s", self.department)
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._running = False
        self.logger.info("Stopping vigilance service")
        if self._poll_task:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
            self._poll_task = None
        self.logger.info("Vigilance service stopped")

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        # Stagger the very first check by a few seconds so the bot finishes
        # its startup sequence before we hit the network.
        await asyncio.sleep(15)
        while self._running:
            try:
                await self._check_and_alert()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error("Unexpected error in vigilance poll loop: %s", e)
            try:
                await asyncio.sleep(self.poll_interval_seconds)
            except asyncio.CancelledError:
                break

    async def _check_and_alert(self) -> None:
        """Fetch vigilance data and send an alert if warranted."""
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, self._fetch_with_retry)
        if info is None:
            return

        color = info['color']

        # Log level changes
        if color != self._last_color:
            self.logger.info(
                "Vigilance level changed for dept %s: %s → %s",
                self.department, self._last_color, color,
            )

        self._last_color = color
        self._last_bulletin_date = info['bulletin_date']

        if self._should_alert(info):
            message = self._format_message(info)
            await self._send_alert(message, info)
            self._last_alert_time = time.monotonic()
            self.logger.info(
                "Vigilance alert sent for dept %s: %s", self.department, color
            )

    def _fetch_with_retry(self) -> Optional[dict[str, Any]]:
        """Fetch vigilance data with exponential-backoff retries (called in executor)."""
        max_retries = 3
        base_delay = 2.0
        transient_errors = (
            http.client.RemoteDisconnected,
            socket.timeout,
            ConnectionResetError,
            ConnectionRefusedError,
            OSError,
            requests.exceptions.RequestException,
        )

        for attempt in range(max_retries):
            try:
                data = fetch_vigilance(self.department)
                if attempt > 0:
                    self.logger.info(
                        "Vigilance fetched after %d attempt(s)", attempt + 1
                    )
                else:
                    self.logger.debug(
                        "Vigilance check dept %s: %s", self.department, data['color']
                    )
                return data
            except transient_errors as e:
                if attempt < max_retries - 1:
                    delay = (base_delay * (2 ** attempt)) + random.uniform(0, 1)
                    self.logger.info(
                        "Vigilance fetch error (%s), retry %d/%d in %.1fs",
                        type(e).__name__, attempt + 1, max_retries, delay,
                    )
                    time.sleep(delay)
                else:
                    self.logger.error(
                        "Vigilance fetch failed after %d attempts: %s: %s",
                        max_retries, type(e).__name__, e,
                    )
                    return None
            except Exception as e:
                self.logger.error(
                    "Unexpected error fetching vigilance for dept %s: %s: %s",
                    self.department, type(e).__name__, e,
                )
                return None
        return None

    # ------------------------------------------------------------------
    # Alert logic
    # ------------------------------------------------------------------

    def _should_alert(self, info: dict[str, Any]) -> bool:
        color = info['color']
        if color not in self.alert_levels:
            return False
        # 0.0 means "no alert sent yet in this run". time.monotonic() starts close to zero
        # on a freshly booted host (containers, CI runners), so an unset reference looked
        # like a throttle still running and the first alert of a run was skipped.
        if self._last_alert_time:
            elapsed = time.monotonic() - self._last_alert_time
            if elapsed < self.alert_throttle:
                self.logger.debug(
                    "Vigilance alert throttled (%.0fs remaining)", self.alert_throttle - elapsed
                )
                return False
        return True

    def _format_message(self, info: dict[str, Any]) -> str:
        """Format a compact LoRa-friendly alert message (≤ 180 chars)."""
        color = info['color']
        emoji = {'Vert': '✅', 'Jaune': '⚠️', 'Orange': '🟠', 'Rouge': '🔴'}.get(color, '🌦️')
        lines = [f"{emoji} VIGILANCE {color.upper()}", f"Dept {self.department}"]
        summary = info.get('summary', '')
        if summary and ':' in summary:
            for part in summary.split('\n')[1:3]:
                stripped = part.strip()
                if stripped.startswith('-'):
                    lines.append(stripped[2:])
        result = '\n'.join(lines)
        if len(result) > 180:
            result = result[:177] + '...'
        return result

    async def _send_alert(self, message: str, info: dict[str, Any]) -> None:
        """Post the alert to the mesh channel and/or external webhooks."""
        if not self.silence_mesh:
            try:
                await self.bot.command_manager.send_channel_message(
                    self.channel, message, scope=self.get_mesh_flood_scope()
                )
            except Exception as e:
                self.logger.error("Failed to send vigilance mesh alert: %s", e)

        url = info.get('url', '')
        if url:
            await self.send_external_notifications(f"{message}\n{url}")
        else:
            await self.send_external_notifications(message)

