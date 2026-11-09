#!/usr/bin/env python3
"""
Vigilance Météo Service for MeshCore Bot

Polls Météo-France vigilance weather alerts for one or several French departments
and posts to a configured channel when the alert level reaches Orange or Rouge.

The level comes from the official DonneesPubliquesVigilance product with the key
in [Vigilance_Service] api_key. Without a key it is read from the JSON endpoint the
site's own JavaScript calls, authenticated with the session cookie the page sets; that
path covers metropolitan France and Corsica only. beautifulsoup4 is kept for the page
itself, whose markup carries no level (the old 'vigilancemeteo' package is unmaintained).

With a key the product is national, so one request answers every department. Without
one the page is fetched once per poll: the cookie it sets is a session cookie, so the
same token answers for every department, and only one request per department follows.
Requests of one poll are spaced by ``request_delay`` (default 2 s) rather than sent as
one burst.
"""

import asyncio
import codecs
import contextlib
import http.client
import random
import re
import socket
import time
import unicodedata
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Optional

import requests
from bs4 import BeautifulSoup, Tag

from modules.utils import get_config_timezone
from modules.vigilance_alert_policy import VigilanceAlertPolicy, fingerprint
from modules.vigilance_cache import AvailabilityNotices, VigilanceCache

from .base_service import BaseServicePlugin

# Département → URL-slug mapping for Météo-France vigilance pages. The site serves
# pages by department name, never by number: /fr/95 is a 404 while /fr/val-d-oise
# answers. A slug is the name folded to ASCII with apostrophes and spaces turned into
# hyphens, so the whole table follows that rule except '90' and '93', spelled as the
# site uses them. City names (the old table) are not slugs and 404.
_DEPT_SLUGS: dict[str, str] = {
    '01': 'ain', '02': 'aisne', '03': 'allier', '04': 'alpes-de-haute-provence',
    '05': 'hautes-alpes', '06': 'alpes-maritimes', '07': 'ardeche', '08': 'ardennes',
    '09': 'ariege', '10': 'aube', '11': 'aude', '12': 'aveyron',
    '13': 'bouches-du-rhone', '14': 'calvados', '15': 'cantal', '16': 'charente',
    '17': 'charente-maritime', '18': 'cher', '19': 'correze', '2A': 'corse-du-sud',
    '2B': 'haute-corse', '21': 'cote-d-or', '22': 'cotes-d-armor', '23': 'creuse',
    '24': 'dordogne', '25': 'doubs', '26': 'drome', '27': 'eure',
    '28': 'eure-et-loir', '29': 'finistere', '30': 'gard', '31': 'haute-garonne',
    '32': 'gers', '33': 'gironde', '34': 'herault', '35': 'ille-et-vilaine',
    '36': 'indre', '37': 'indre-et-loire', '38': 'isere', '39': 'jura',
    '40': 'landes', '41': 'loir-et-cher', '42': 'loire', '43': 'haute-loire',
    '44': 'loire-atlantique', '45': 'loiret', '46': 'lot', '47': 'lot-et-garonne',
    '48': 'lozere', '49': 'maine-et-loire', '50': 'manche', '51': 'marne',
    '52': 'haute-marne', '53': 'mayenne', '54': 'meurthe-et-moselle', '55': 'meuse',
    '56': 'morbihan', '57': 'moselle', '58': 'nievre', '59': 'nord',
    '60': 'oise', '61': 'orne', '62': 'pas-de-calais', '63': 'puy-de-dome',
    '64': 'pyrenees-atlantiques', '65': 'hautes-pyrenees', '66': 'pyrenees-orientales',
    '67': 'bas-rhin', '68': 'haut-rhin', '69': 'rhone', '70': 'haute-saone',
    '71': 'saone-et-loire', '72': 'sarthe', '73': 'savoie', '74': 'haute-savoie',
    '75': 'paris', '76': 'seine-maritime', '77': 'seine-et-marne', '78': 'yvelines',
    '79': 'deux-sevres', '80': 'somme', '81': 'tarn', '82': 'tarn-et-garonne',
    '83': 'var', '84': 'vaucluse', '85': 'vendee', '86': 'vienne',
    '87': 'haute-vienne', '88': 'vosges', '89': 'yonne', '90': 'terr-de-belfort',
    '91': 'essonne', '92': 'hauts-de-seine', '93': 'seine-st-denis', '94': 'val-de-marne',
    '95': 'val-d-oise',
    # Overseas departments. Saint-Pierre-et-Miquelon (975) has no vigilance page.
    '971': 'guadeloupe', '972': 'martinique', '973': 'guyane', '974': 'la-reunion',
    '976': 'mayotte',
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

# Official vigilance product (DonneesPubliquesVigilance). One call answers every
# department, so the department is picked out of the returned national product.
_VIGILANCE_API_URL = 'https://public-api.meteofrance.fr/public/DPVigilance/v1/cartevigilance/encours'

# The JSON endpoint the site's own JavaScript calls, for a department page. The markup
# carries no level and the endpoint refuses an anonymous request: it takes the token the
# page hands over as the `mfsession` cookie, which is that token written in ROT-13.
# Metropolitan France and Corsica only, the overseas departments are another product.
_VIGILANCE_API_V3 = 'https://rwg.meteofrance.com/wsft/v3'

# Pause between two requests of the same poll. The endpoint is the site's own, with no
# published rate limit, so a list of departments is spread out instead of fired as one burst:
# ten departments take about 20 s instead of under a second, which is nothing on an 8 h poll.
# Configured by `request_delay`.
_REQUEST_DELAY_SECONDS = 2.0

# Color codes used by the product: 1 is the lowest level.
_COLOR_IDS = {1: 'Vert', 2: 'Jaune', 3: 'Orange', 4: 'Rouge'}

_PHENOMENON_NAMES = {
    '1': 'Vent', '2': 'Pluie-inondation', '3': 'Orages', '4': 'Crues',
    '5': 'Neige-verglas', '6': 'Canicule', '7': 'Grand froid',
    '8': 'Avalanches', '9': 'Vagues-submersion',
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


def _slugify(name: str) -> str:
    """Fold a department name into the slug Météo-France uses in its URLs."""
    folded = unicodedata.normalize('NFD', name.strip().lower())
    folded = ''.join(ch for ch in folded if unicodedata.category(ch) != 'Mn')
    return folded.replace("'", '-').replace(' ', '-')


def _dept_slug(department: str) -> str:
    """Resolve a department code ('95', '2A') to its page slug.

    Numbers must be in ``_DEPT_SLUGS``: the site has no numeric path, so an unmapped
    number could only ever 404 and is reported instead. Anything that is not a number
    is taken as a department name and folded into a slug.

    Raises:
        ValueError: when the code is a number with no vigilance page.
    """
    code = department.strip().upper()
    slug = _DEPT_SLUGS.get(code) or _DEPT_SLUGS.get(code.zfill(2))
    if slug:
        return slug
    if code.isdigit():
        raise ValueError(
            f"Unknown department '{department}': Météo-France has no vigilance page for it"
        )
    return _slugify(department)


def normalize_department(raw: str) -> str:
    """Fold one department to the form Météo-France writes: '7' → '07', '2a' → '2A'.

    A name is left as it is, since ``_dept_slug`` is what turns it into a slug.
    """
    code = raw.strip().upper()
    return code.zfill(2) if code.isdigit() else code


def parse_departments(value: str) -> list[str]:
    """Split the configured ``department`` value, which accepts a comma-separated list."""
    return [normalize_department(part) for part in value.split(',') if part.strip()]


def _page_url(code: str) -> str:
    return f"https://vigilance.meteofrance.fr/fr/{_dept_slug(code)}"


def _parse_api_datetime(value: Any) -> datetime:
    """Parse the product's update_time; do not invent a fresh publication date."""
    try:
        if isinstance(value, str) and 'T' in value:
            parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
            return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed
    except (ValueError, TypeError):
        pass
    raise ValueError('Données de vigilance indisponibles : date du bulletin manquante ou invalide')


def _read_color(value: Any) -> str:
    """Missing or unknown data is not evidence of a green vigilance level."""
    if isinstance(value, bool) or str(value) not in {'1', '2', '3', '4'}:
        raise ValueError('Données de vigilance indisponibles : niveau manquant ou invalide')
    return _COLOR_IDS[int(value)]


def _phenomena(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    result = []
    for item in items:
        color = _read_color(item.get('phenomenon_max_color_id'))
        if color != 'Vert':
            result.append({'id': str(item['phenomenon_id']), 'color': color})
    return sorted(result, key=lambda p: p['id'])


def _api_summary(color: str, phenomena: list[dict[str, Any]]) -> str:
    """Build the compact summary the message formatter expects."""
    if color == 'Vert':
        return "Pas de vigilance particulière"
    lines = [f"Vigilance {color.lower()} :"]
    for item in phenomena:
        pid = str(item.get('phenomenon_id', ''))
        lines.append(f"- {_PHENOMENON_NAMES.get(pid, pid)}")
    return '\n'.join(lines)


def _fetch_product(api_key: str, timeout: int) -> dict[str, Any]:
    """Fetch the national vigilance product. One call answers every department."""
    response = requests.get(
        _VIGILANCE_API_URL,
        headers={**_VIGILANCE_HEADERS, 'apikey': api_key},
        timeout=timeout,
    )
    response.raise_for_status()
    try:
        payload = response.json()
    except requests.exceptions.JSONDecodeError:
        # The portal answers HTML when the key is missing or refused.
        raise ValueError(
            "The vigilance API did not return JSON: check [Vigilance_Service] api_key"
        ) from None
    return payload.get('product') or {}


def _product_entry(department: str, product: dict[str, Any]) -> dict[str, Any]:
    """Read one department out of the national product.

    Raises:
        ValueError: when the product carries no block for that department.
    """
    code = normalize_department(department)
    domain = None
    for period in product.get('periods') or []:
        for item in (period.get('timelaps') or {}).get('domain_ids') or []:
            if str(item.get('domain_id', '')).upper() == code:
                domain = item
                break
        if domain:
            break
    if domain is None:
        raise ValueError(f"The vigilance product has no block for department '{department}'")

    color = _read_color(domain.get('max_color_id'))
    active = [
        p for p in domain.get('phenomenon_items') or []
        if _read_color(p.get('phenomenon_max_color_id')) != 'Vert'
    ]

    result = {
        'color': color,
        'summary': _api_summary(color, active),
        'bulletin_date': _parse_api_datetime(product.get('update_time')),
        'url': _page_url(code),
        'phenomena': _phenomena(active),
        'issued_at': _parse_api_datetime(product.get('update_time')).replace(tzinfo=timezone.utc).timestamp(),
        'bulletin': deepcopy(product),
    }
    if period.get('end_validity_time'):
        result['valid_until'] = _parse_api_datetime(period['end_validity_time']).replace(tzinfo=timezone.utc).timestamp()
    return result


def fetch_vigilance_api(department: str, api_key: str, timeout: int = 10) -> dict[str, Any]:
    """
    Fetch the current level for a department from the official vigilance product.

    The product is national, so the department is read out of its block in the
    period J timelaps. Returns the same dict as fetch_vigilance.
    Raises requests.exceptions.RequestException on network errors and
    ValueError when the product carries no block for that department.
    """
    return _product_entry(department, _fetch_product(api_key, timeout))


def _session_token(page_response: requests.Response) -> str:
    """Turn the page's `mfsession` cookie into the Bearer token the JSON endpoint wants."""
    cookie = page_response.cookies.get('mfsession', '')
    return codecs.encode(cookie, 'rot13') if cookie else ''


def fetch_vigilance_site_api(
    department: str, token: str, url: str, timeout: int = 10, delay: float = 0.0
) -> dict[str, Any]:
    """
    Read the level for a department from the JSON endpoint the site itself calls.

    `delay` pauses before the request, so a batch of departments is not sent as one burst.

    Returns the same dict as fetch_vigilance.
    Raises requests.exceptions.RequestException on network errors.
    """
    if delay > 0:
        time.sleep(delay)
    response = requests.get(
        f'{_VIGILANCE_API_V3}/warning/full',
        params={'domain': department},
        headers={**_VIGILANCE_HEADERS, 'Authorization': f'Bearer {token}'},
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()

    if str(data.get('domain_id', '')).upper() != normalize_department(department):
        raise ValueError('Données de vigilance indisponibles : département incorrect')
    color = _read_color(data.get('color_max'))
    active: list[dict[str, Any]] = []
    for item in data.get('timelaps') or []:
        colors = []
        for timestep in item.get('timelaps_items') or []:
            _read_color(timestep.get('color_id'))
            colors.append(int(timestep['color_id']))
        active.append({
            'phenomenon_id': str(item.get('phenomenon_id', '')),
            'phenomenon_max_color_id': max(colors) if colors else 1,
        })

    stamp = data.get('update_time')
    if isinstance(stamp, bool) or not str(stamp).isdigit() or int(stamp) <= 0:
        raise ValueError('Données de vigilance indisponibles : date du bulletin manquante ou invalide')
    result = {
        'color': color,
        'summary': _api_summary(color, [p for p in active if p['phenomenon_max_color_id'] > 1]),
        'bulletin_date': datetime.fromtimestamp(int(stamp)),
        'url': url,
        'phenomena': _phenomena(active),
        'issued_at': int(stamp),
        'bulletin': deepcopy(data),
    }
    if data.get('end_validity_time') is not None:
        until = data['end_validity_time']
        if isinstance(until, bool) or not str(until).isdigit() or int(until) <= int(stamp):
            raise ValueError('Données de vigilance indisponibles : validité du bulletin incorrecte')
        result['valid_until'] = int(until)
    return result


def _spaced_page(url: str, timeout: int, delay: float) -> requests.Response:
    """Fetch a page, after the pause that separates it from the previous request."""
    if delay > 0:
        time.sleep(delay)
    return requests.get(url, headers=_VIGILANCE_HEADERS, timeout=timeout)


def _page_entry(response: requests.Response, url: str) -> dict[str, Any]:
    """Build an entry from the page markup, which carries no level and reports Vert."""
    soup = BeautifulSoup(response.content, 'html.parser')
    color = _extract_color(soup)
    return {
        'color': color,
        'summary': _extract_summary(soup, color),
        'bulletin_date': _extract_bulletin_date(soup),
        'url': url,
    }


def fetch_vigilance_multi(
    codes: list[str], api_key: str = '', timeout: int = 10, delay: float = 0.0
) -> dict[str, dict[str, Any]]:
    """
    Fetch current vigilance data for several French departments from Météo-France.

    Uses the official product when ``api_key`` is set, otherwise the JSON endpoint
    the site's JavaScript calls. Page markup is not a fallback for structured data.

    With a key the product is national, so one request answers the whole list. Without one,
    the page is fetched once: the ``mfsession`` cookie it sets is a session cookie, so the
    same token answers for every department and only one request per department follows,
    which is one request per department instead of two. A missing session cookie makes
    the fetch fail without inventing a vigilance level.

    ``delay`` is the pause between two requests of the same poll: the page fetch is not
    preceded by one, and every request after it is. So a list of N costs N+1 requests and N
    pauses.

    Returns code -> dict, each entry with keys: color, summary, bulletin_date, url.
    Raises ValueError for an empty list, for an unknown department, or for an overseas
    department without a key, and requests.exceptions.RequestException on network errors.
    """
    codes = [normalize_department(code) for code in codes]
    if not codes:
        raise ValueError("No department to check")

    if api_key:
        product = _fetch_product(api_key, timeout)
        return {code: _product_entry(code, product) for code in codes}

    response = requests.get(_page_url(codes[0]), headers=_VIGILANCE_HEADERS, timeout=timeout)
    response.raise_for_status()
    token = _session_token(response)
    if not token:
        # The page is rendered by JavaScript. Its empty markup must never clear
        # an alert just because the structured endpoint cannot be authenticated.
        raise ValueError('Données de vigilance indisponibles : accès au bulletin non obtenu')

    entries: dict[str, dict[str, Any]] = {}
    for code in codes:
        url = _page_url(code)
        if len(code) != 2:
            # Overseas department: the site's endpoint has no block for it, only the
            # official product does, and that one needs a key.
            raise ValueError(
                f"Department {code} is overseas, so its level needs [Vigilance_Service] api_key"
            )
        entries[code] = fetch_vigilance_site_api(code, token, url, timeout, delay)

    return entries


def fetch_vigilance(
    department: str, api_key: str = '', timeout: int = 10, delay: float = 0.0
) -> dict[str, Any]:
    """
    Fetch current vigilance data for a French department from Météo-France.

    The single-department form of fetch_vigilance_multi, same sources in the same order.

    Returns a dict with keys: color, summary, bulletin_date, url.
    Raises ValueError for an unknown department, or for an overseas department without
    a key, and requests.exceptions.RequestException on network errors.
    """
    code = normalize_department(department)
    return fetch_vigilance_multi([code], api_key, timeout, delay)[code]


class VigilanceService(BaseServicePlugin):
    """
    Polls Météo-France vigilance alerts for one or several French departments and
    posts to a channel when Orange or Rouge vigilance is active.

    One alert per department that is at an alert level, each with its own throttle, so
    a level in one department does not silence the alert of another one.

    Config section: ``[Vigilance_Service]``
    """

    config_section = 'Vigilance_Service'
    description = "Météo-France weather vigilance alert monitor (France)"

    settings_schema = [
        {
            "key": "department",
            "label": "Department number(s)",
            "type": "str",
            "default": "75",
            "help": "French department number(s) to monitor, comma-separated "
                    "(e.g. 75, or 75,77,78,91,92,93,94,95,60,27).",
            "required": True,
        },
        {
            "key": "api_key",
            "label": "Météo-France API key",
            "type": "str",
            "default": "",
            "help": "DonneesPubliquesVigilance key from portail-api.meteofrance.fr. Optional: "
                    "without it the level comes from the site's own endpoint, which covers "
                    "metropolitan France and Corsica only, so an overseas department listed here "
                    "is not polled. With a key the national product answers the whole list in one "
                    "request.",
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
            "key": "request_delay",
            "label": "Delay between requests",
            "type": "float",
            "min": 0,
            "default": _REQUEST_DELAY_SECONDS,
            "unit": "s",
            "help": "Pause between two requests of the same poll. The site's endpoint has no "
                    "published rate limit, so a list of departments is spread out rather than "
                    "sent as one burst. 0 sends them back to back.",
        },
        {
            "key": "alert_throttle",
            "label": "Alert throttle",
            "type": "int",
            "min": 60,
            "default": 3600,
            "unit": "s",
            "help": "Minimum seconds between successive alerts for the same department.",
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
            "key": "notify_on_change", "label": "Alert on changes", "type": "bool",
            "default": False,
            "help": "Send changes immediately, remember sent alerts across restarts, and repeat only at the reminder interval.",
        },
        {
            "key": "reminder_interval", "label": "Reminder interval", "type": "int",
            "default": 86400, "min": 3600, "unit": "s",
            "help": "Repeat an unchanged active vigilance after this interval in change mode.",
        },
        {
            "key": "offline_cache", "label": "Keep bulletins for offline use", "type": "bool",
            "default": False,
            "help": "Keep complete bulletins locally. Announce failed updates once and label cached vigi answers as historical.",
        },
        {
            "key": "group_similar_alerts", "label": "Group similar alerts", "type": "bool",
            "default": False,
            "help": "In change mode, group departments due an alert with the same color and phenomena.",
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

        self.api_key: str = self.bot.config.get(section, 'api_key', fallback='').strip()
        codes = parse_departments(self.bot.config.get(section, 'department', fallback='75'))
        # Without a key the site's endpoint has no block for an overseas department, so a
        # code of three digits can never be read: it is named in the log and left out of the
        # poll rather than fetched for a 404.
        overseas = [code for code in codes if len(code) != 2]
        self.departments: list[str] = codes if self.api_key else [c for c in codes if len(c) == 2]
        # `vigi` with no argument answers for this one, so the reply stays inside the RF budget.
        self.department: str = self.departments[0] if self.departments else ''
        self.channel: str = self.bot.config.get(section, 'channel', fallback='#general').strip()
        poll_ms = self.bot.config.getint(section, 'poll_interval', fallback=28800000)
        self.poll_interval_seconds: float = poll_ms / 1000.0
        self.alert_throttle: int = self.bot.config.getint(section, 'alert_throttle', fallback=3600)
        self.request_delay: float = self.bot.config.getfloat(
            section, 'request_delay', fallback=_REQUEST_DELAY_SECONDS
        )
        raw_levels = self.bot.config.get(section, 'alert_levels', fallback='Orange,Rouge')
        self.alert_levels: list[str] = [lvl.strip() for lvl in raw_levels.split(',') if lvl.strip()]
        self.silence_mesh: bool = self.bot.config.getboolean(
            section, 'silence_mesh_output', fallback=False
        )
        self.notify_on_change = self.bot.config.getboolean(section, 'notify_on_change', fallback=False)
        self.alert_policy = VigilanceAlertPolicy(
            getattr(self.bot, 'db_manager', None), self.logger, self.channel,
            self.get_mesh_flood_scope(),
            reminder_seconds=max(3600, self.bot.config.getint(section, 'reminder_interval', fallback=86400)),
        )
        self.offline_cache = self.bot.config.getboolean(section, 'offline_cache', fallback=False)
        self.group_similar_alerts = self.bot.config.getboolean(section, 'group_similar_alerts', fallback=False)
        self.bulletin_cache = VigilanceCache(getattr(self.bot, 'db_manager', None), self.logger)
        self.availability = AvailabilityNotices(
            getattr(self.bot, 'db_manager', None), self.logger, self.alert_policy.namespace,
        )
        self.display_tz = get_config_timezone(self.bot.config, self.logger)[0]

        # Runtime state, one entry per department: a level or a throttle held for one
        # department must not be read as the state of another.
        self._running = False
        self._poll_task: Optional[asyncio.Task] = None
        self._last_color: dict[str, Optional[str]] = {}
        self._last_bulletin_date: dict[str, datetime] = {}
        self._last_alert_time: dict[str, float] = {}

        self.logger.info(
            "Vigilance service initialized: depts=%s channel=%s interval=%.0fs throttle=%ds "
            "levels=%s delay=%.1fs change_mode=%s reminder=%ds offline_cache=%s group_similar=%s",
            ', '.join(self.departments), self.channel, self.poll_interval_seconds,
            self.alert_throttle, ', '.join(self.alert_levels), self.request_delay,
            self.notify_on_change, self.alert_policy.reminder_seconds,
            self.offline_cache, self.group_similar_alerts,
        )
        if not self.api_key:
            self.logger.warning(
                "Vigilance: no api_key configured, so the level comes from the site's own "
                "endpoint, which covers metropolitan France and Corsica only"
            )
            if overseas:
                self.logger.warning(
                    "Vigilance: %s is overseas and needs api_key, so it is not polled",
                    ', '.join(overseas),
                )

    async def start(self) -> None:
        if not self.enabled:
            self.logger.info("Vigilance service is disabled, not starting")
            return
        self._running = True
        self.logger.info("Starting vigilance service for departments %s", ', '.join(self.departments))
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
        """Fetch current bulletins, transmit due alerts and report unavailable updates."""
        if not self.departments:
            return

        loop = asyncio.get_event_loop()
        infos = await loop.run_in_executor(None, self._fetch_with_retry)
        if not infos:
            if self.offline_cache:
                await self._notify_availability(self.departments)
            return

        pending = []
        available = set()
        for code, info in infos.items():
            if code not in self.departments:
                continue
            if info.get('color') not in _COLOR_IDS.values() or (
                info.get('valid_until') is not None and info['valid_until'] <= time.time()
            ):
                self.logger.warning('Ignoring invalid or expired vigilance data for dept %s', code)
                continue
            if self.offline_cache and not self.bulletin_cache.put(code, info, time.time()):
                continue
            available.add(code)
            color = info['color']
            previous = self._last_color.get(code)

            # Log level changes
            if color != previous:
                self.logger.info(
                    "Vigilance level changed for dept %s: %s → %s", code, previous, color
                )

            self._last_color[code] = color
            self._last_bulletin_date[code] = info['bulletin_date']

            if self.notify_on_change:
                reason = self.alert_policy.reason(code, info, self.alert_levels, time.time())
                if reason:
                    if self.group_similar_alerts:
                        pending.append((code, info, reason))
                        continue
                    message = self._format_change_message(code, info, reason)
                    if await self._send_alert(message, info):
                        self.alert_policy.record_sent(code, info, time.time())
                        self.logger.info('Vigilance %s transmitted for dept %s: %s', reason, code, color)
                    else:
                        self.logger.warning('Vigilance %s not transmitted for dept %s; state retained', reason, code)
            elif self._should_alert(code, info):
                message = self._format_message(code, info)
                if await self._send_alert(message, info):
                    self._last_alert_time[code] = time.monotonic()
                    self.logger.info("Vigilance alert sent for dept %s: %s", code, color)

        if pending:
            await self._send_grouped_alerts(pending)
        if self.offline_cache:
            await self._notify_availability([code for code in self.departments if code not in available])

    async def _send_grouped_alerts(self, pending: list[tuple[str, dict[str, Any], str]]) -> None:
        groups: dict[str, list[tuple[str, dict[str, Any], str]]] = {}
        for event in pending:
            key = 'clear' if event[2] == 'clear' else fingerprint(event[1])
            groups.setdefault(key, []).append(event)
        priority = {'Rouge': 0, 'Orange': 1, 'Jaune': 2, 'Vert': 3}
        for events in sorted(groups.values(), key=lambda g: priority[g[0][1]['color']]):
            codes = sorted(event[0] for event in events)
            reason = ('clear' if events[0][1]['color'] == 'Vert' else
                      'reminder' if all(e[2] == 'reminder' for e in events) else 'change')
            info = dict(events[0][1])
            info['url'] = '\n'.join(sorted({e[1]['url'] for e in events if e[1].get('url')}))
            message = self._format_change_message('/'.join(codes), info, reason,
                                                  label='Depts' if len(codes) > 1 else 'Dept')
            if await self._send_alert(message, info):
                for code, data, _ in events:
                    self.alert_policy.record_sent(code, data, time.time())
                self.logger.info('Vigilance group transmitted for depts %s: %s', ','.join(codes), info['color'])
            else:
                self.logger.warning('Vigilance group not transmitted for depts %s; state retained', ','.join(codes))

    async def _notify_availability(self, missing: list[str]) -> None:
        now = time.time()
        statuses = {code: self.bulletin_cache.status(code, now) for code in sorted(missing)}
        if not self.availability.due(statuses):
            return
        if not missing:
            message = 'Actualisation de la vigilance rétablie.'
        else:
            lines = [f"Vigilance non actualisable - Depts {'/'.join(sorted(missing))}."]
            expired = [code for code, status in statuses.items() if status == 'expired']
            if expired:
                lines.append(f"Bulletins périmés : {'/'.join(expired)}.")
            old = [code for code, status in statuses.items() if status == 'old']
            if old:
                lines.append(f"Bulletins anciens : {'/'.join(old)}.")
            records = [self.bulletin_cache.get(code) for code in missing]
            if all(record is not None for record in records):
                oldest = min(record['fetched_at'] for record in records if record is not None)
                date = datetime.fromtimestamp(oldest, self.display_tz).strftime('%d/%m à %H:%M')
                lines.append(f'Dernière récupération : {date}.')
            elif not any(records):
                lines.append('Aucun bulletin conservé.')
            lines.append('Situation actuelle inconnue.')
            message = '\n'.join(lines)
        if await self._send_alert(message, {}):
            self.availability.record_sent(statuses)

    def _fetch_with_retry(self) -> Optional[dict[str, dict[str, Any]]]:
        """Fetch the levels with exponential-backoff retries (called in executor)."""
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
                data = fetch_vigilance_multi(
                    self.departments, self.api_key, delay=self.request_delay
                )
                if attempt > 0:
                    self.logger.info(
                        "Vigilance fetched after %d attempt(s)", attempt + 1
                    )
                else:
                    self.logger.debug(
                        "Vigilance check %s",
                        ', '.join(f"{code} {entry['color']}" for code, entry in data.items()),
                    )
                return data
            except transient_errors as e:
                if attempt < max_retries - 1:
                    backoff = (base_delay * (2 ** attempt)) + random.uniform(0, 1)
                    self.logger.info(
                        "Vigilance fetch error (%s), retry %d/%d in %.1fs",
                        type(e).__name__, attempt + 1, max_retries, backoff,
                    )
                    time.sleep(backoff)
                else:
                    self.logger.error(
                        "Vigilance fetch failed after %d attempts: %s: %s",
                        max_retries, type(e).__name__, e,
                    )
                    return None
            except Exception as e:
                self.logger.error(
                    "Unexpected error fetching vigilance for depts %s: %s: %s",
                    ', '.join(self.departments), type(e).__name__, e,
                )
                return None
        return None

    # ------------------------------------------------------------------
    # Alert logic
    # ------------------------------------------------------------------

    def _should_alert(self, code: str, info: dict[str, Any]) -> bool:
        color = info['color']
        if color not in self.alert_levels:
            return False
        # The throttle is per department: an alert sent for one department would otherwise
        # hold the reference that says whether another one is still throttled.
        # 0.0 means "no alert sent yet in this run". time.monotonic() starts close to zero
        # on a freshly booted host (containers, CI runners), so an unset reference looked
        # like a throttle still running and the first alert of a run was skipped.
        last = self._last_alert_time.get(code, 0.0)
        if last:
            elapsed = time.monotonic() - last
            if elapsed < self.alert_throttle:
                self.logger.debug(
                    "Vigilance alert throttled for dept %s (%.0fs remaining)",
                    code, self.alert_throttle - elapsed
                )
                return False
        return True

    def _format_message(self, code: str, info: dict[str, Any]) -> str:
        """Format a compact LoRa-friendly alert message (≤ 180 chars) for one department."""
        color = info['color']
        emoji = {'Vert': '✅', 'Jaune': '⚠️', 'Orange': '🟠', 'Rouge': '🔴'}.get(color, '🌦️')
        lines = [f"{emoji} VIGILANCE {color.upper()}", f"Dept {code}"]
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

    def _format_change_message(self, code: str, info: dict[str, Any], reason: str, *, label: str = 'Dept') -> str:
        if reason == 'clear':
            return f'✅ Fin de vigilance - {label} {code} : retour au vert.'
        color = info['color']
        emoji = {'Jaune': '⚠️', 'Orange': '🟠', 'Rouge': '🔴'}[color]
        prefix = 'Rappel : ' if reason == 'reminder' else ''
        lines = [f'{emoji} {prefix}Vigilance {color.upper()} - {label} {code}']
        for item in info.get('phenomena', []):
            name = _PHENOMENON_NAMES.get(item['id'], 'Phénomène ' + item['id'])
            lines.append(f"{name} : {item['color'].lower()}")
        return '\n'.join(lines)

    async def _send_alert(self, message: str, info: dict[str, Any]) -> bool:
        """Post the alert to the mesh channel and/or external webhooks."""
        sent = False
        if not self.silence_mesh:
            try:
                sent = bool(await self.bot.command_manager.send_channel_message(
                    self.channel, message, scope=self.get_mesh_flood_scope(),
                    skip_user_rate_limit=True,
                ))
            except Exception as e:
                self.logger.error("Failed to send vigilance mesh alert: %s", e)

        url = info.get('url', '')
        try:
            await self.send_external_notifications(f"{message}\n{url}" if url else message)
        except Exception:
            self.logger.warning('Failed to send external vigilance notification', exc_info=True)
        return sent if not self.silence_mesh else self.has_external_notification_targets()

