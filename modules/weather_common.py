"""Behavior the wx (NOAA) and gwx (Open-Meteo) commands share.

``WeatherCommandMixin`` goes before ``BaseCommand`` in a weather command's
bases and expects the BaseCommand API (``bot``, ``logger``, ``translate``,
``send_response``, ``get_max_message_length``, ``response_translator``). Each
command sets ``translation_ns`` to its own catalog namespace.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Union

from .clients.mqtt_weather import (
    get_mqtt_weather_topic,
    load_mqtt_weather_format_config,
    mqtt_weather_display_for_topic,
)
from .utils import format_temperature_high_low

Number = Union[int, float]


def load_open_meteo_model(config: Any, logger: Any) -> Optional[str]:
    """[Weather] weather_model for Open-Meteo's ``models`` parameter.

    Unset means ``best_match``; explicitly blank means None (omit the parameter
    and let Open-Meteo choose). Anything outside ``[a-z0-9_,.-]`` falls back to
    ``best_match`` with a warning.
    """
    if config.has_option('Weather', 'weather_model'):
        model = config.get('Weather', 'weather_model', fallback='').strip().lower()
        if not model:
            return None
    else:
        model = 'best_match'
    if not re.fullmatch(r'[a-z0-9_,.-]+', model):
        logger.warning(f"Invalid weather_model '{model}', using 'best_match'")
        return 'best_match'
    return model


# Open-Meteo model selection and response cache shared by gwx and Weather_Service.

OPEN_METEO_FRANCE_MODEL = 'meteofrance_arome_france'
# AROME only publishes about two days, so longer requests stay on the configured model.
OPEN_METEO_FRANCE_MODEL_MAX_FORECAST_DAYS = 2
# Cached responses answer identical requests for this long before the API is asked again.
OPEN_METEO_CACHE_FRESH_HOURS = 8.0
# Hourly replies name the hours after the payload's own "now"; reusing an older one
# would list hours that have already passed, so their fresh window is much shorter.
OPEN_METEO_HOURLY_FRESH_HOURS = 1.0
# Row lifetime in the cache, and the oldest response the failure fallback may serve.
OPEN_METEO_CACHE_MAX_AGE_HOURS = 24.0
OPEN_METEO_CACHE_TYPE = 'openmeteo_weather'

# Rough bounding box of metropolitan France (Corsica inside, every overseas department
# outside). The Météo-France models answer 400 outside their domain, so a geocoded "FR"
# that is actually Guadeloupe or Réunion must not get the France model; neighbouring
# countries inside the box are harmless because AROME covers and forecasts them.
_METRO_FRANCE_BOUNDS = (41.2, 51.3, -6.2, 9.7)  # lat_min, lat_max, lon_min, lon_max


def is_metro_france(latitude: Any, longitude: Any) -> bool:
    """Whether these coordinates fall in the metropolitan-France box."""
    try:
        lat, lon = float(latitude), float(longitude)
    except (TypeError, ValueError):
        return False
    lat_min, lat_max, lon_min, lon_max = _METRO_FRANCE_BOUNDS
    return lat_min <= lat <= lat_max and lon_min <= lon <= lon_max


def select_open_meteo_model(
    configured_model: Optional[str],
    *,
    latitude: Any,
    longitude: Any,
    forecast_days: int,
    country_code: Optional[str] = None,
) -> Optional[str]:
    """Pick the Open-Meteo ``models`` value for one request.

    An explicit [Weather] weather_model other than ``best_match`` always wins.
    Otherwise a location that looks like metropolitan France (geocoded country code
    ``FR`` inside the metropolitan box, or no country information at all and
    coordinates inside the box) gets the Météo-France AROME model for forecasts of
    two days or less; everything else keeps the configured default.
    """
    if configured_model and configured_model != 'best_match':
        return configured_model
    country = (country_code or '').strip().lower()
    if country and country != 'fr':
        return configured_model
    if forecast_days <= OPEN_METEO_FRANCE_MODEL_MAX_FORECAST_DAYS and is_metro_france(latitude, longitude):
        return OPEN_METEO_FRANCE_MODEL
    return configured_model


def open_meteo_cache_key(params: dict) -> str:
    """Cache key for one Open-Meteo request; coordinates rounded to ~100 m."""
    lat: Any = params.get('latitude')
    lon: Any = params.get('longitude')
    try:
        lat = round(float(lat), 3)
        lon = round(float(lon), 3)
    except (TypeError, ValueError):
        pass
    parts = (
        lat, lon,
        params.get('models', ''),
        params.get('temperature_unit', ''),
        params.get('wind_speed_unit', ''),
        params.get('precipitation_unit', ''),
        params.get('forecast_days', ''),
        params.get('current', ''),
        params.get('daily', ''),
        params.get('hourly', ''),
    )
    digest = hashlib.sha256('|'.join(str(part) for part in parts).encode('utf-8')).hexdigest()[:20]
    return f"openmeteo:{digest}"


def read_open_meteo_cache(
    logger: Any, db_manager: Any, params: dict
) -> tuple[Optional[dict], Optional[float]]:
    """Cached Open-Meteo response for the same request: ``(data, age_hours)`` or ``(None, None)``."""
    if db_manager is None:
        return None, None
    try:
        entry = db_manager.get_cached_json(open_meteo_cache_key(params), OPEN_METEO_CACHE_TYPE)
    except Exception as e:
        logger.debug(f"Open-Meteo cache read failed: {e}")
        return None, None
    if not isinstance(entry, dict):
        return None, None
    data = entry.get('data')
    fetched_at = entry.get('fetched_at')
    if not isinstance(data, dict) or not isinstance(fetched_at, str):
        return None, None
    try:
        fetched = datetime.fromisoformat(fetched_at)
    except ValueError:
        return None, None
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    age_hours = (datetime.now(timezone.utc) - fetched).total_seconds() / 3600.0
    if age_hours < 0:
        return None, None
    return data, age_hours


def open_meteo_cache_is_usable(data: dict, age_hours: Optional[float], max_age_hours: float) -> bool:
    """Whether a cached response may answer instead of a fresh forecast.

    It must be at most ``max_age_hours`` old and still describe the location's
    current local day: a payload cached yesterday would label yesterday's
    temperatures as today.
    """
    if age_hours is None or age_hours > max_age_hours:
        return False
    offset = data.get('utc_offset_seconds')
    reference = (data.get('current') or {}).get('time') or ((data.get('daily') or {}).get('time') or [None])[0]
    if offset is None or not reference:
        return False
    try:
        cached_day = datetime.fromisoformat(str(reference)[:10]).date()
        local_now = datetime.now(timezone.utc) + timedelta(seconds=int(offset))
    except (ValueError, TypeError):
        return False
    return cached_day == local_now.date()


def store_open_meteo_payload(logger: Any, db_manager: Any, params: dict, data: dict) -> None:
    """Cache a successful Open-Meteo response, stamped with its fetch time."""
    if db_manager is None or not isinstance(data, dict):
        return
    try:
        db_manager.cache_json(
            open_meteo_cache_key(params),
            {'fetched_at': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'data': data},
            OPEN_METEO_CACHE_TYPE,
            cache_hours=int(OPEN_METEO_CACHE_MAX_AGE_HOURS),
        )
    except Exception as e:
        logger.debug(f"Open-Meteo cache write failed: {e}")


def load_open_meteo_units(config: Any, logger: Any) -> tuple[str, str, str]:
    """[Weather] temperature_unit, wind_speed_unit and precipitation_unit for Open-Meteo.

    Lowercased; a value Open-Meteo does not accept falls back to fahrenheit,
    mph or inch with a warning. The result doubles as a translation key for the
    unit's display label.
    """
    temperature_unit = config.get('Weather', 'temperature_unit', fallback='fahrenheit').lower()
    wind_speed_unit = config.get('Weather', 'wind_speed_unit', fallback='mph').lower()
    precipitation_unit = config.get('Weather', 'precipitation_unit', fallback='inch').lower()
    if temperature_unit not in ('fahrenheit', 'celsius'):
        logger.warning(f"Invalid temperature_unit '{temperature_unit}', using 'fahrenheit'")
        temperature_unit = 'fahrenheit'
    if wind_speed_unit not in ('mph', 'kmh', 'ms', 'kn'):
        logger.warning(f"Invalid wind_speed_unit '{wind_speed_unit}', using 'mph'")
        wind_speed_unit = 'mph'
    if precipitation_unit not in ('inch', 'mm'):
        logger.warning(f"Invalid precipitation_unit '{precipitation_unit}', using 'inch'")
        precipitation_unit = 'inch'
    return temperature_unit, wind_speed_unit, precipitation_unit


# 16-point compass labels, clockwise from north, and the arrow for each 45° sector.
_COMPASS_16 = (
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
)
_ARROWS_8 = ("⬆️", "↗️", "➡️", "↘️", "⬇️", "↙️", "⬅️", "↖️")


class WeatherCommandMixin:
    # Catalog namespace for this command's strings, e.g. "commands.wx".
    translation_ns: str = ""

    # Provided by BaseCommand.
    bot: Any
    logger: Any
    response_translator: Any
    translate: Any
    send_response: Any
    get_max_message_length: Any
    get_config_value: Any
    record_execution: Any
    _get_custom_wxsim_source: Any
    _get_companion_location: Any
    _get_bot_location: Any
    default_city: Any
    default_state: Any
    default_country: Any

    @staticmethod
    def _coordinates_query(lat: float, lon: float) -> str:
        """A "lat,lon" location for these coordinates, in fixed decimals.

        Plain str() of a float can give "1e-05", which the coordinate pattern
        does not match, so the point would be geocoded as a place name.
        """
        return f"{lat:.5f},{lon:.5f}"

    @staticmethod
    def _parse_forecast_suffix(
        location_parts: list[str], max_days: int, *, allow_hourly: bool
    ) -> tuple[list[str], str, int]:
        """Strip a trailing forecast option from the location words.

        "tomorrow", "hourly" (when allowed), "7day"/"7-day", "Nd" or a bare "N"
        with 2 <= N <= max_days. Returns (remaining words, forecast type, days);
        anything else stays part of the location and the type is "default".
        """
        forecast_type = "default"
        num_days = 7  # Default for multi-day forecast
        if len(location_parts) > 0:
            last_part = location_parts[-1].lower()
            if last_part == "tomorrow":
                forecast_type = "tomorrow"
                location_parts = location_parts[:-1]
            elif allow_hourly and last_part == "hourly":
                forecast_type = "hourly"
                location_parts = location_parts[:-1]
            elif last_part in ["7day", "7-day"]:
                forecast_type = "multiday"
                num_days = 7
                location_parts = location_parts[:-1]
            else:
                nd_match = re.fullmatch(r"(\d+)d", last_part)
                if nd_match:
                    days = int(nd_match.group(1))
                    if 2 <= days <= max_days:
                        forecast_type = "multiday"
                        num_days = days
                        location_parts = location_parts[:-1]
                elif last_part.isdecimal():
                    days = int(last_part)
                    if 2 <= days <= max_days:
                        forecast_type = "multiday"
                        num_days = days
                        location_parts = location_parts[:-1]
        return location_parts, forecast_type, num_days

    def _is_custom_source_name(self, word: str) -> bool:
        """Whether *word* names a configured custom.wxsim.* or custom.mqtt_weather.* source.

        Such a name is a place even when it looks like an option ("custom.wxsim.hourly").
        """
        return bool(self._get_custom_mqtt_weather_topic(word) or self._get_custom_wxsim_source(word))

    def _split_option_only(self, parts: list[str], max_days: int) -> tuple[list[str], Optional[str], str, int]:
        """Pull a forecast option given without a location ("wx hourly", "gwx 5d", "wx alerts").

        Returns (parts without it, the option word or None, forecast type, days), so the
        no-location fallbacks (custom default source, the sender's position, default_city,
        the bot's position) can apply the option instead of showing usage.
        """
        if len(parts) == 2:
            word = parts[1]
            if self._is_custom_source_name(word):
                return parts, None, "default", 7
            if word.lower() == "alerts":
                return parts[:1], word, "alerts", 7
            rest, forecast_type, num_days = self._parse_forecast_suffix(parts[1:], max_days, allow_hourly=True)
            if not rest and forecast_type != "default":
                return parts[:1], parts[1], forecast_type, num_days
        return parts, None, "default", 7

    def _format_high_low(self, high: Optional[Number], low: Optional[Number], temp_symbol: str) -> str:
        """Format high/low using [Weather] temperature_*_format templates."""
        return format_temperature_high_low(self.bot.config, high, low, temp_symbol, self.logger,
                                           translator=self.response_translator)

    def _get_custom_mqtt_weather_topic(self, location: Optional[str] = None) -> Optional[str]:
        """MQTT topic for custom.mqtt_weather.<name> (see get_mqtt_weather_topic)."""
        return get_mqtt_weather_topic(self.bot.config, location)

    def _mqtt_weather_line(self, topic: str, forecast_type: str, location_name: Optional[str]) -> str:
        """Format the cached MQTT weather payload for ``topic``, or the matching error text."""
        ns = self.translation_ns
        if forecast_type != "default":
            return self.translate(f"{ns}.mqtt_forecast_not_supported")
        fmt = load_mqtt_weather_format_config(self.bot.config)
        cache = getattr(self.bot, "mqtt_weather_cache", None)
        text, err = mqtt_weather_display_for_topic(topic, cache, fmt)
        if text is not None:
            return f"{location_name}: {text}" if location_name else text
        return self._mqtt_weather_error_key(err)

    def _mqtt_weather_error_key(self, err: Optional[str]) -> str:
        """The translated reply for an MQTT weather lookup error (overridable)."""
        ns = self.translation_ns
        if err == "no_cache":
            return self.translate(f"{ns}.mqtt_weather_no_subscriber")
        if err in ("no_data", "empty_payload", "empty_after_sanitize"):
            return self.translate(f"{ns}.mqtt_weather_no_data")
        if err == "stale":
            return self.translate(f"{ns}.mqtt_weather_stale")
        detail = (err or "unknown").replace("_", " ")
        return self.translate(f"{ns}.mqtt_weather_payload_error", detail=detail)

    def _count_display_width(self, text: str) -> int:
        """Count UTF-8 byte length of text. Matches RF packet byte limit from get_max_message_length()."""
        return len(text.encode('utf-8'))

    @staticmethod
    def _hour_label(start_time_str: str) -> str:
        """12-hour label for an ISO start time; empty when missing or unparseable."""
        if not start_time_str:
            return ""
        try:
            hour = datetime.fromisoformat(start_time_str.replace('Z', '+00:00')).hour
        except (ValueError, TypeError):
            return ""
        return f"{hour % 12 or 12}{'AM' if hour < 12 else 'PM'}"

    @staticmethod
    def _without_arrow(direction: str) -> str:
        """A wind direction label without its leading arrow emoji ("↖️WNW" -> "WNW").

        Hourly lines drop the arrow to keep an extra hour in the reply; the
        letters (translated or not) stay.
        """
        return re.sub(r"^\W+", "", direction)

    @staticmethod
    def _short_hourly_description(description: str) -> str:
        """Long hourly descriptions keep their first three words, or 18 characters."""
        if len(description) > 18:
            words = description.split()
            return ' '.join(words[:3]) if len(words) > 3 else description[:18]
        return description

    def _pack_hourly_lines(self, hourly_lines: Iterable[str], max_length: int) -> str:
        """Pack consecutive whole hours into one UTF-8 byte budget."""
        lines: list[str] = []
        for line in hourly_lines:
            if self._count_display_width("\n".join(lines + [line])) > max_length:
                break
            lines.append(line)
        return "\n".join(lines) or self.translate(f'{self.translation_ns}.hourly_not_available')

    @staticmethod
    async def _pace_reply(message: Any, seconds: float) -> None:
        """Wait between the parts of a reply so the TX limiter lets the next through.

        A scheduled {cmd:...} render collects the parts without transmitting, so it
        does not wait: the pause would only eat into its render timeout.
        """
        if getattr(message, 'capture_sink', None) is None:
            await asyncio.sleep(seconds)

    async def _send_multiday_forecast(self, message: Any, forecast_text: str) -> bool:
        """Send a multi-day forecast, packing whole lines into as few messages as fit.

        A line too long for one message goes out on its own. The first message
        goes through the per-user rate limit; the ones after it skip it and are
        spaced 2 s apart. Returns whether every message was sent.
        """
        max_length = self.get_max_message_length(message)
        lines = [line.strip() for line in forecast_text.split('\n') if line.strip()]
        if not lines:
            return False
        if self._count_display_width(forecast_text) <= max_length:
            return bool(await self.send_response(message, forecast_text))

        parts: list[str] = []
        current_message = ""
        for line in lines:
            test_message = current_message + "\n" + line if current_message else line
            if self._count_display_width(test_message) <= max_length:
                current_message = test_message
                continue
            if current_message:
                parts.append(current_message)
            # A single line too long for one message goes out anyway (the bot splits it).
            current_message = line
        if current_message:
            parts.append(current_message)

        sent_all = True
        for i, part in enumerate(parts):
            if i > 0:
                await self._pace_reply(message, 2.0)
            sent = await self.send_response(message, part, skip_user_rate_limit=(i > 0))
            if not sent:
                sent_all = False
                break
        return sent_all

    async def _reply_from_mqtt(
        self,
        message: Any,
        topic: str,
        forecast_type: str,
        location: Optional[str],
        *,
        split_multiday: bool = True,
    ) -> bool:
        """Answer from a custom.mqtt_weather source; errors become the error reply."""
        try:
            self.record_execution(message.sender_id)
            weather_data = self._mqtt_weather_line(topic, forecast_type, location)
            if split_multiday and forecast_type == "multiday":
                return await self._send_multiday_forecast(message, weather_data)
            return bool(await self.send_response(message, weather_data))
        except Exception as e:
            self.logger.error(f"Error reading MQTT weather: {e}")
            await self.send_response(message, self.translate(f"{self.translation_ns}.error", error=str(e)))
            return True

    async def _reply_from_wxsim(
        self,
        message: Any,
        source: str,
        forecast_type: str,
        num_days: int,
        location: Optional[str] = None,
    ) -> bool:
        """Answer from a custom.wxsim source; errors become the error reply."""
        try:
            self.record_execution(message.sender_id)
            weather_data = await self._fetch_wxsim(source, forecast_type, num_days, message, location)
            if forecast_type == "multiday":
                return await self._send_multiday_forecast(message, weather_data)
            return bool(await self.send_response(message, weather_data))
        except Exception as e:
            self.logger.error(f"Error fetching WXSIM weather: {e}")
            await self.send_response(message, self.translate(f"{self.translation_ns}.error", error=str(e)))
            return True

    async def _fetch_wxsim(
        self, source: str, forecast_type: str, num_days: int, message: Any, location: Optional[str]
    ) -> str:
        """Fetch and format a WXSIM source off the event loop (each command's own way)."""
        raise NotImplementedError

    async def _fallback_place_name(self, lat: float, lon: float) -> Optional[str]:
        """A place name for logging a fallback position, or None to log the coordinates."""
        return None

    def _default_city_query(self) -> Optional[str]:
        """``default_city[, default_state][, default_country]``, or None when unset."""
        if not self.default_city:
            return None
        location_parts = [self.default_city]
        if self.default_state:
            location_parts.append(self.default_state)
        if self.default_country:
            location_parts.append(self.default_country)
        return ", ".join(location_parts)

    async def _no_location_fallback(self, message: Any) -> tuple[Optional[str], bool]:
        """Where to forecast when the request names no place.

        Tries the sender's position, then default_city, then (when
        use_bot_location_when_no_location is set) the bot's position. Returns
        (location query or None, whether it is the sender's position).
        """
        companion_location = self._get_companion_location(message)
        if companion_location:
            # Use coordinates directly to avoid re-geocoding issues
            location_str = self._coordinates_query(*companion_location)
            display_name = await self._fallback_place_name(companion_location[0], companion_location[1])
            if display_name:
                self.logger.info(f"Using companion location: {display_name} ({companion_location[0]}, {companion_location[1]})")
            else:
                self.logger.info(f"Using companion coordinates: {location_str}")
            return location_str, True

        default_city = self._default_city_query()
        if default_city:
            self.logger.info(f"Using default city (no args): {default_city}")
            return default_city, False

        use_bot = self.get_config_value(
            'Wx_Command',
            'use_bot_location_when_no_location',
            fallback=False,
            value_type='bool',
        )
        bot_loc = self._get_bot_location() if use_bot else None
        if bot_loc:
            location_str = self._coordinates_query(*bot_loc)
            display_name = await self._fallback_place_name(bot_loc[0], bot_loc[1])
            if display_name:
                self.logger.info(
                    f"Using bot location (no args): {display_name} ({bot_loc[0]}, {bot_loc[1]})"
                )
            else:
                self.logger.info(f"Using bot coordinates (no args): {location_str}")
            return location_str, False

        if use_bot:
            self.logger.debug(
                "use_bot_location_when_no_location enabled but bot_latitude/bot_longitude "
                "not set; showing usage"
            )
        else:
            self.logger.debug("No companion/default city location found, showing usage")
        return None, False

    async def _send_weather_reply(self, message: Any, weather_data: Any, forecast_type: str) -> bool:
        """Send a provider's answer: two parts with alerts, split by day, or one message."""
        if isinstance(weather_data, tuple) and weather_data[0] == "multi_message":
            # Send weather data first; if it was refused (rate limit, send failure),
            # do not send its second part on its own.
            if not await self.send_response(message, weather_data[1]):
                return False

            # Wait for bot TX rate limiter to allow next message
            rate_limit = self.bot.config.getfloat('Bot', 'bot_tx_rate_limit_seconds', fallback=1.0)
            # Use a conservative sleep time to avoid rate limiting
            sleep_time = max(rate_limit + 1.0, 2.0)  # At least 2 seconds, or rate_limit + 1 second
            await self._pace_reply(message, sleep_time)

            # Second part of the same reply: the reply limiter already let the first through.
            return bool(await self.send_response(message, weather_data[2], skip_user_rate_limit=True))
        if forecast_type == "multiday":
            # Use message splitting for multi-day forecasts
            return await self._send_multiday_forecast(message, weather_data)
        return bool(await self.send_response(message, weather_data))
