#!/usr/bin/env python3
"""Solar, lunar, and satellite condition helpers.

This module is an independent implementation based on the public data formats
documented by HamQSL, NOAA SWPC, N2YO, and PyEphem:

* https://www.hamqsl.com/solarxml.php
* https://services.swpc.noaa.gov/text/drap_global_frequencies.txt
* https://www.n2yo.com/api/
* https://rhodesmill.org/pyephem/quick.html

The short text formats are retained for compatibility with MeshCore Bot's
command handlers and constrained radio messages.
"""

from __future__ import annotations

import configparser
import logging
from collections.abc import Iterable
from datetime import datetime, timezone
from time import time
from typing import Any
from xml.etree import ElementTree

import ephem
import requests

logger = logging.getLogger(__name__)

DEFAULT_LATITUDE = 40.7128
DEFAULT_LONGITUDE = -74.0060
DEFAULT_N2YO_API_KEY = ""
DEFAULT_URL_TIMEOUT = 10
DEFAULT_ZULU_TIME = False
ERROR_FETCHING_DATA = "Error fetching data"

HAMQSL_SOLAR_URL = "https://www.hamqsl.com/solarxml.php"
NOAA_DRAP_URL = "https://services.swpc.noaa.gov/text/drap_global_frequencies.txt"
N2YO_BASE_URL = "https://api.n2yo.com/rest/v1/satellite"

_config: configparser.ConfigParser | None = None


def set_config(config: configparser.ConfigParser | None) -> None:
    """Set the application configuration used by subsequent calls."""

    global _config
    _config = config


def get_config_value(section: str, key: str, fallback: Any) -> Any:
    """Read and type-convert a configuration value using ``fallback``'s type."""

    if _config is None or not _config.has_section(section):
        return fallback

    raw_value = _config.get(section, key, fallback=None)
    if raw_value is None:
        return fallback

    try:
        if isinstance(fallback, bool):
            return str(raw_value).strip().lower() in {"1", "yes", "true", "on"}
        if isinstance(fallback, int):
            return int(raw_value)
        if isinstance(fallback, float):
            return float(raw_value)
        return raw_value
    except (TypeError, ValueError):
        logger.warning("Invalid value for [%s] %s; using default", section, key)
        return fallback


def _request_timeout() -> int:
    timeout = get_config_value("Solar_Config", "url_timeout", DEFAULT_URL_TIMEOUT)
    return max(1, int(timeout))


def _fetch_solar_data() -> ElementTree.Element:
    response = requests.get(HAMQSL_SOLAR_URL, timeout=_request_timeout())
    response.raise_for_status()
    root = ElementTree.fromstring(response.text)
    solar_data = root.find("solardata")
    if solar_data is None:
        raise ValueError("HamQSL response has no solardata element")
    return solar_data


def _element_text(parent: ElementTree.Element, tag: str) -> str:
    element = parent.find(tag)
    if element is None or element.text is None:
        return ""
    return element.text


def _band_rows(solar_data: ElementTree.Element) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for element in solar_data.findall("./calculatedconditions/band"):
        period = element.get("time", "").strip().lower()
        name = element.get("name", "").strip()
        condition = (element.text or "").strip()
        if period and name and condition:
            rows.append((period, name, condition))
    return rows


def hf_band_conditions() -> str:
    """Return HamQSL HF conditions as one compact assignment per band."""

    try:
        rows = _band_rows(_fetch_solar_data())
        if not rows:
            raise ValueError("HamQSL response has no band conditions")
        return "\n".join(f"{period[0]}{name}={condition}" for period, name, condition in rows)
    except Exception:
        logger.exception("Unable to retrieve HamQSL HF propagation report")
        return ERROR_FETCHING_DATA


def solar_conditions() -> str:
    """Return the primary HamQSL solar indices in a readable multiline form."""

    try:
        data = _fetch_solar_data()
        fields = (
            ("A-Index", "aindex"),
            ("K-Index", "kindex"),
            ("Sunspots", "sunspots"),
            ("X-Ray Flux", "xray"),
            ("Solar Flux", "solarflux"),
            ("Signal Noise", "signalnoise"),
        )
        return "\n".join(f"{label}: {_element_text(data, tag)}" for label, tag in fields)
    except Exception:
        logger.exception("Solar: Unable to retrieve current solar indices")
        return ERROR_FETCHING_DATA


def solar_conditions_condensed() -> str:
    """Return the primary HamQSL indices on one line."""

    try:
        data = _fetch_solar_data()
        return (
            f"A:{_element_text(data, 'aindex')} "
            f"K:{_element_text(data, 'kindex')} "
            f"Sun:{_element_text(data, 'sunspots')} "
            f"Flux:{_element_text(data, 'solarflux')} "
            f"Xray:{_element_text(data, 'xray')} "
            f"Noise:{_element_text(data, 'signalnoise')}"
        )
    except Exception:
        logger.exception("Solar: Error fetching condensed solar conditions")
        return ERROR_FETCHING_DATA


def _group_bands(rows: Iterable[tuple[str, str, str]], period: str) -> str:
    grouped: dict[str, list[str]] = {}
    for row_period, name, condition in rows:
        if row_period == period:
            grouped.setdefault(condition, []).append(name)
    ranges = []
    for condition, names in grouped.items():
        band_range = names[0] if len(names) == 1 else f"{names[0]}-{names[-1]}"
        ranges.append(f"{band_range}{condition}")
    return " ".join(ranges)


def hf_band_conditions_condensed() -> str:
    """Group bands with equal conditions into a single day/night line."""

    try:
        rows = _band_rows(_fetch_solar_data())
        if not rows:
            raise ValueError("HamQSL response has no band conditions")
        day = _group_bands(rows, "day")
        night = _group_bands(rows, "night")
        return f"D:{day} N:{night}"
    except Exception:
        logger.exception("Solar: Error fetching condensed HF band conditions")
        return ERROR_FETCHING_DATA


def drap_xray_conditions() -> str:
    """Return NOAA SWPC's current global DRAP X-ray message."""

    try:
        response = requests.get(NOAA_DRAP_URL, timeout=_request_timeout())
        response.raise_for_status()
        for line in response.text.splitlines():
            label, separator, value = line.partition(":")
            if separator and label.lstrip("# ").strip().upper() == "X-RAY MESSAGE":
                return value.strip() or "No X-ray data found"
        return "No X-ray data found"
    except Exception:
        logger.exception("Solar: Error fetching NOAA DRAP X-ray conditions")
        return ERROR_FETCHING_DATA


def _observer(lat: float, lon: float) -> ephem.Observer:
    observer = ephem.Observer()
    # PyEphem interprets strings as degrees and floats as radians.
    observer.lat = str(lat)
    observer.lon = str(lon)
    observer.date = ephem.now()
    return observer


def _coordinates(lat: float | None, lon: float | None) -> tuple[float, float]:
    latitude = get_config_value("Bot", "bot_latitude", DEFAULT_LATITUDE) if lat is None else lat
    longitude = get_config_value("Bot", "bot_longitude", DEFAULT_LONGITUDE) if lon is None else lon
    latitude = float(latitude)
    longitude = float(longitude)
    if not -90 <= latitude <= 90:
        raise ValueError("latitude must be between -90 and 90")
    if not -180 <= longitude <= 180:
        raise ValueError("longitude must be between -180 and 180")
    return latitude, longitude


def _local_datetime(value: ephem.Date) -> datetime:
    utc_value = ephem.Date(value).datetime()
    if utc_value.tzinfo is None:
        utc_value = utc_value.replace(tzinfo=timezone.utc)
    return utc_value.astimezone()


def _format_event(value: ephem.Date, *, include_month: bool = False) -> str:
    use_24_hour = get_config_value("Solar_Config", "use_zulu_time", DEFAULT_ZULU_TIME)
    if include_month:
        pattern = "%a %b %d %H:%M" if use_24_hour else "%a %b %d %I:%M%p"
    else:
        pattern = "%a %d %H:%M" if use_24_hour else "%a %d %I:%M%p"
    return _local_datetime(value).strftime(pattern)


def _duration_parts(seconds: float) -> tuple[int, int]:
    total_minutes = max(0, int(seconds // 60))
    return divmod(total_minutes, 60)


def get_sun(lat: float | None = None, lon: float | None = None) -> str:
    """Calculate the next solar events and current position for a location."""

    try:
        latitude, longitude = _coordinates(lat, lon)
        observer = _observer(latitude, longitude)
        sun = ephem.Sun(observer)
        sun_is_up = float(sun.alt) >= 0

        sunrise = observer.next_rising(sun)
        sunset = observer.next_setting(sun)
        previous_sunrise = observer.previous_rising(sun)
        next_sunset = observer.next_setting(sun, start=sunrise)

        if sun_is_up:
            daylight_seconds = float(sunset - previous_sunrise) * 86400
            remaining_seconds = float(sunset - observer.date) * 86400
        else:
            daylight_seconds = float(next_sunset - sunrise) * 86400

        # Rise/set searches leave the body positioned at the event; restore the
        # current position before reporting azimuth and altitude.
        sun.compute(observer)
        daylight_hours, daylight_minutes = _duration_parts(daylight_seconds)
        azimuth = float(sun.az) * 180.0 / ephem.pi
        altitude = float(sun.alt) * 180.0 / ephem.pi

        if sun_is_up:
            remaining_hours, remaining_minutes = _duration_parts(remaining_seconds)
            lines = [
                f"SunSet: {_format_event(sunset)}",
                f"Rise: {_format_event(sunrise)}",
                f"Daylight: {daylight_hours}h {daylight_minutes}m",
                f"Remaining: {remaining_hours}h {remaining_minutes}m",
                f"Azimuth: {azimuth:.2f}°",
                f"Altitude: {altitude:.2f}°",
            ]
        else:
            lines = [
                f"SunRise: {_format_event(sunrise)}",
                f"Set: {_format_event(next_sunset)}",
                f"Daylight: {daylight_hours}h {daylight_minutes}m",
                f"Azimuth: {azimuth:.2f}°",
            ]
        return "\n".join(lines)
    except Exception:
        logger.exception("Solar: Error calculating Sun conditions")
        return ERROR_FETCHING_DATA


_MOON_PHASES = (
    (0.0625, "New Moon🌑"),
    (0.1875, "Waxing Crescent🌒"),
    (0.3125, "First Quarter🌓"),
    (0.4375, "Waxing Gibbous🌔"),
    (0.5625, "Full Moon🌕"),
    (0.6875, "Waning Gibbous🌖"),
    (0.8125, "Last Quarter🌗"),
    (0.9375, "Waning Crescent🌘"),
    (1.0, "New Moon🌑"),
)


def _moon_phase_name(moment: ephem.Date) -> str:
    previous_new = ephem.previous_new_moon(moment)
    next_new = ephem.next_new_moon(moment)
    cycle_fraction = float(moment - previous_new) / float(next_new - previous_new)
    for upper_bound, name in _MOON_PHASES:
        if cycle_fraction < upper_bound:
            return name
    return _MOON_PHASES[-1][1]


def get_moon(lat: float | None = None, lon: float | None = None) -> str:
    """Calculate lunar rise/set times, phase, and upcoming principal phases."""

    try:
        latitude, longitude = _coordinates(lat, lon)
        observer = _observer(latitude, longitude)
        moon = ephem.Moon(observer)
        illumination = float(moon.phase)
        moonrise = observer.next_rising(moon)
        moonset = observer.next_setting(moon)
        full_moon = ephem.next_full_moon(observer.date)
        new_moon = ephem.next_new_moon(observer.date)

        return "\n".join(
            (
                f"MoonRise:{_format_event(moonrise)}",
                f"Set:{_format_event(moonset)}",
                f"Phase:{_moon_phase_name(observer.date)} @:{illumination:.2f}%",
                f"FullMoon:{_format_event(full_moon, include_month=True)}",
                f"NewMoon:{_format_event(new_moon, include_month=True)}",
            )
        )
    except Exception:
        logger.exception("Solar: Error calculating Moon conditions")
        return ERROR_FETCHING_DATA


def _unix_local(timestamp: int | float) -> str:
    use_24_hour = get_config_value("Solar_Config", "use_zulu_time", DEFAULT_ZULU_TIME)
    pattern = "%a %d %H:%M" if use_24_hour else "%a %d %I:%M%p"
    return datetime.fromtimestamp(timestamp).strftime(pattern)


def get_next_satellite_pass(
    satellite: str,
    lat: float | None = None,
    lon: float | None = None,
    use_visual: bool = False,
) -> str:
    """Return the next N2YO visual or radio pass in the legacy compact format."""

    try:
        satellite_text = str(satellite).strip()
        try:
            satellite_number = int(satellite_text)
        except (TypeError, ValueError):
            return "Provide NORAD# example use:🛰️satpass 25544,33591"
        if satellite_number <= 0:
            return "Provide NORAD# example use:🛰️satpass 25544,33591"
        satellite_id = str(satellite_number)

        latitude, longitude = _coordinates(lat, lon)
        api_key = str(get_config_value("External_Data", "n2yo_api_key", DEFAULT_N2YO_API_KEY)).strip()
        if not api_key:
            logger.error("System: Missing N2YO API key; get one at https://www.n2yo.com/login/")
            return "not configured, bug your sysop"

        pass_kind = "visualpasses" if use_visual else "radiopasses"
        threshold = 60 if use_visual else 0
        url = (
            f"{N2YO_BASE_URL}/{pass_kind}/{satellite_id}/{latitude}/{longitude}"
            f"/0/10/{threshold}/&apiKey={api_key}"
        )
        response = requests.get(url, timeout=DEFAULT_URL_TIMEOUT)
        response.raise_for_status()
        payload = response.json()

        info = payload.get("info") or {}
        passes = payload.get("passes") or []
        satellite_name = info.get("satname") or satellite_id
        if not passes:
            qualifier = "visual" if use_visual else "radio"
            return f"{satellite_name} has no {qualifier} passes in the next 10 days from this location"

        now = time()
        next_pass = next((candidate for candidate in passes if int(candidate["startUTC"]) >= now), None)
        if next_pass is None:
            qualifier = "visual" if use_visual else "radio"
            return f"{satellite_name} has no {qualifier} passes in the next 10 days from this location"

        start = int(next_pass["startUTC"])
        end = int(next_pass["endUTC"])
        duration = int(next_pass.get("duration", end - start))
        if duration > 7200:
            return f"{satellite_name}: GEO (no LEO pass)"

        minutes, seconds = divmod(max(0, duration), 60)
        duration_text = f"{minutes}m{seconds}s" if minutes else f"{seconds}s"
        return (
            f"{satellite_name} @{_unix_local(start)} "
            f"Az:{next_pass['startAzCompass']} for{duration_text}, "
            f"MaxEl:{next_pass['maxEl']}° "
            f"Set@{_unix_local(end)} Az:{next_pass['endAzCompass']}"
        )
    except Exception:
        logger.exception("Solar: Error fetching satellite pass")
        return ERROR_FETCHING_DATA
