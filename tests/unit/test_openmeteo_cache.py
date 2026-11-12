"""Open-Meteo response cache and metropolitan-France model selection (gwx, wx fallback,
Weather_Service daily forecast)."""

import asyncio
import configparser
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

import pytest
import requests

from modules.commands.alternatives.wx_international import GlobalWxCommand
from modules.service_plugins.weather_service import WeatherService
from modules.weather_common import (
    OPEN_METEO_CACHE_FRESH_HOURS,
    OPEN_METEO_CACHE_MAX_AGE_HOURS,
    OPEN_METEO_FRANCE_MODEL,
    open_meteo_cache_is_usable,
    open_meteo_cache_key,
    read_open_meteo_cache,
    select_open_meteo_model,
    store_open_meteo_payload,
)

MODULE = "modules.commands.alternatives.wx_international"

PARIS = (48.8566, 2.3522)
SEATTLE = (47.6062, -122.3321)
GUATELUPE = (16.24, -61.53)


class FakeDB:
    """Dict-backed stand-in for bot.db_manager's generic JSON cache."""

    def __init__(self):
        self.rows = {}
        self.cache_hours = None

    def get_cached_json(self, cache_key, cache_type):
        row = self.rows.get((cache_key, cache_type))
        return json.loads(row) if row else None

    def cache_json(self, cache_key, cache_value, cache_type, cache_hours=720):
        self.rows[(cache_key, cache_type)] = json.dumps(cache_value)
        self.cache_hours = cache_hours

    def age_all(self, hours):
        """Make every stored entry this many hours old."""
        old = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
        for key, row in list(self.rows.items()):
            entry = json.loads(row)
            entry['fetched_at'] = old
            self.rows[key] = json.dumps(entry)

    def cached_count(self):
        return len(self.rows)


# ---------------------------------------------------------------------------
# Model selection

@pytest.mark.parametrize(
    "configured, expected",
    [
        (None, OPEN_METEO_FRANCE_MODEL),          # weather_model = (blank)
        ('best_match', OPEN_METEO_FRANCE_MODEL),  # unset default
        ('ecmwf_ifs025', 'ecmwf_ifs025'),         # explicit model wins
    ],
)
def test_paris_short_forecast_uses_arome(configured, expected):
    model = select_open_meteo_model(configured, latitude=PARIS[0], longitude=PARIS[1], forecast_days=2)
    assert model == expected


def test_paris_multiday_keeps_configured_default():
    # AROME only reaches ~2 days; longer requests must keep the default model.
    model = select_open_meteo_model('best_match', latitude=PARIS[0], longitude=PARIS[1], forecast_days=8)
    assert model == 'best_match'


def test_non_france_position_keeps_default():
    assert select_open_meteo_model('best_match', latitude=SEATTLE[0], longitude=SEATTLE[1], forecast_days=2) == 'best_match'


def test_known_foreign_country_rejects_arome():
    # Brussels is inside the metropolitan-France box, but its geocoded country rules AROME out.
    model = select_open_meteo_model('best_match', latitude=50.85, longitude=4.35, forecast_days=2, country_code='be')
    assert model == 'best_match'


def test_overseas_france_rejects_arome():
    # Guadeloupe geocodes to country_code "fr"; the metropolitan box must still reject it
    # because the Météo-France models answer 400 there.
    model = select_open_meteo_model('best_match', latitude=GUATELUPE[0], longitude=GUATELUPE[1], forecast_days=2, country_code='fr')
    assert model == 'best_match'


def test_corsica_accepts_arome():
    model = select_open_meteo_model('best_match', latitude=41.92, longitude=8.74, forecast_days=2)
    assert model == OPEN_METEO_FRANCE_MODEL


def test_is_metro_france_bounds():
    from modules.weather_common import is_metro_france

    assert is_metro_france(50.95, 1.85)     # Calais
    assert is_metro_france(41.92, 8.74)     # Ajaccio
    assert not is_metro_france(GUATELUPE[0], GUATELUPE[1])
    assert not is_metro_france(-21.1, 55.5)  # Réunion
    assert not is_metro_france(51.51, -0.12)  # London
    assert not is_metro_france(None, 2.35)


def test_select_model_rejects_non_numeric_coordinates():
    assert select_open_meteo_model('best_match', latitude=None, longitude=None, forecast_days=2) == 'best_match'


# ---------------------------------------------------------------------------
# Cache store/read roundtrip

def _params(models='best_match'):
    return {
        'latitude': PARIS[0], 'longitude': PARIS[1], 'models': models,
        'temperature_unit': 'celsius', 'wind_speed_unit': 'kmh', 'precipitation_unit': 'mm',
        'forecast_days': 2, 'current': 'temperature_2m', 'daily': 'weather_code',
    }


def test_store_read_roundtrip(mock_logger):
    db = FakeDB()
    store_open_meteo_payload(mock_logger, db, _params(), {'current': {'temperature_2m': 12}})
    data, age = read_open_meteo_cache(mock_logger, db, _params())
    assert data == {'current': {'temperature_2m': 12}}
    assert age is not None and age < 0.01
    assert db.cache_hours == int(OPEN_METEO_CACHE_MAX_AGE_HOURS)


def test_read_ignores_foreign_entries(mock_logger):
    db = FakeDB()
    db.rows[(open_meteo_cache_key(_params()), 'openmeteo_weather')] = json.dumps({'fetched_at': 'nope'})
    assert read_open_meteo_cache(mock_logger, db, _params()) == (None, None)
    # A plain Mock (characterization tests pass Mock bot.db_manager) is not a cached entry.
    assert read_open_meteo_cache(mock_logger, Mock(), _params()) == (None, None)


def test_cache_key_rounds_coordinates_and_tracks_params():
    near = dict(_params(), latitude=48.8567, longitude=2.3523)
    assert open_meteo_cache_key(near) == open_meteo_cache_key(_params())
    assert open_meteo_cache_key(dict(_params(), latitude=48.9)) != open_meteo_cache_key(_params())
    assert open_meteo_cache_key(dict(_params(), models='ecmwf_ifs025')) != open_meteo_cache_key(_params())
    assert open_meteo_cache_key(dict(_params(), forecast_days=8)) != open_meteo_cache_key(_params())


def test_usable_windows():
    payload = {'utc_offset_seconds': 3600, 'current': {'time': datetime.now(timezone.utc).isoformat(timespec="minutes")}}
    assert open_meteo_cache_is_usable(payload, 7.0, OPEN_METEO_CACHE_FRESH_HOURS) is True
    assert open_meteo_cache_is_usable(payload, 9.0, OPEN_METEO_CACHE_FRESH_HOURS) is False
    assert open_meteo_cache_is_usable(payload, 20.0, OPEN_METEO_CACHE_MAX_AGE_HOURS) is True
    assert open_meteo_cache_is_usable(payload, None, OPEN_METEO_CACHE_FRESH_HOURS) is False


def test_usable_rejects_other_local_day():
    # A payload cached yesterday evening is fresh enough by age but must not label
    # yesterday's temperatures as today.
    offset = 3600
    yesterday = (datetime.now(timezone.utc) + timedelta(seconds=offset)).date() - timedelta(days=1)
    payload = {'utc_offset_seconds': offset, 'current': {'time': f"{yesterday.isoformat()}T22:30"}}
    assert open_meteo_cache_is_usable(payload, 3.0, OPEN_METEO_CACHE_MAX_AGE_HOURS) is False


# ---------------------------------------------------------------------------
# gwx integration: fresh reuse, failure fallback, expiry

def _payload():
    """A minimal complete Open-Meteo response for the current local time (+24 h hourly)."""
    offset = 3600
    local_now = datetime.now(timezone.utc) + timedelta(seconds=offset)
    hour = local_now.replace(minute=0, second=0, microsecond=0)
    hourly_times = [(hour + timedelta(hours=i)).isoformat(timespec="minutes") for i in range(24)]
    return {
        'latitude': PARIS[0], 'longitude': PARIS[1], 'utc_offset_seconds': offset,
        'timezone': 'Europe/Paris',
        'current': {
            'time': local_now.isoformat(timespec="minutes"), 'temperature_2m': 12.5,
            'relative_humidity_2m': 78, 'apparent_temperature': 11.0, 'precipitation': 0.0,
            'weather_code': 2, 'wind_speed_10m': 14.2, 'wind_direction_10m': 250,
            'wind_gusts_10m': 30.1, 'dewpoint_2m': 8.0, 'visibility': 20000,
            'surface_pressure': 1012.3, 'is_day': 1,
        },
        'current_units': {
            'time': 'iso8601', 'temperature_2m': '°C', 'relative_humidity_2m': '%',
            'apparent_temperature': '°C', 'precipitation': 'mm', 'weather_code': 'wmo code',
            'wind_speed_10m': 'km/h', 'wind_direction_10m': '°', 'wind_gusts_10m': 'km/h',
            'dewpoint_2m': '°C', 'visibility': 'm', 'surface_pressure': 'hPa', 'is_day': '',
        },
        'daily': {
            'time': [hour.date().isoformat(), (hour + timedelta(days=1)).date().isoformat()],
            'weather_code': [2, 61], 'temperature_2m_max': [13.4, 11.0], 'temperature_2m_min': [6.2, 4.1],
            'precipitation_sum': [0.0, 1.2], 'precipitation_probability_max': [20, 70],
            'wind_speed_10m_max': [18.0, 22.0], 'wind_gusts_10m_max': [35.0, 44.0],
        },
        'hourly': {
            'time': hourly_times, 'temperature_2m': [12.0 + i * 0.1 for i in range(24)],
            'weather_code': [2] * 24, 'wind_speed_10m': [14.0] * 24,
            'wind_direction_10m': [250] * 24, 'wind_gusts_10m': [30.0] * 24,
            'precipitation_probability': [10] * 24, 'is_day': [1] * 24,
        },
    }


def _response(payload, ok=True):
    return Mock(ok=ok, status_code=200 if ok else 503, json=Mock(return_value=payload))


def _gwx(weather=None):
    config = configparser.ConfigParser()
    config.read_dict({"Weather": {"temperature_unit": "celsius", "wind_speed_unit": "kmh",
                                  "precipitation_unit": "mm", **(weather or {})},
                      "Gwx_Command": {}, "Bot": {}})
    bot = Mock()
    bot.config = config
    bot.logger = Mock()
    bot.translator.translate.side_effect = lambda key, **kwargs: key
    return GlobalWxCommand(bot)


def _call(cmd, get_mock, lat, lon, **kwargs):
    with patch(f"{MODULE}.requests.get", get_mock):
        return cmd.get_open_meteo_weather(lat, lon, **kwargs)


def test_identical_request_is_answered_from_cache_once():
    cmd = _gwx()
    cmd.db_manager = FakeDB()
    payload = _payload()
    get1 = Mock(return_value=_response(payload))
    first = _call(cmd, get1, *PARIS)
    get2 = Mock(return_value=_response(payload))
    second = _call(cmd, get2, *PARIS)
    assert get1.call_count == 1
    assert get2.call_count == 0  # Served from the 8 h cache without touching the API
    assert first == second
    assert first != "commands.gwx.error_fetching"


def test_api_failure_falls_back_to_cached_data():
    cmd = _gwx()
    cmd.db_manager = FakeDB()
    _call(cmd, Mock(return_value=_response(_payload())), *PARIS)
    # Past the 8 h fresh window the API is asked again...
    cmd.db_manager.age_all(OPEN_METEO_CACHE_FRESH_HOURS + 1)

    failing = Mock(side_effect=requests.exceptions.ConnectionError("boom"))
    text = _call(cmd, failing, *PARIS)
    # ...it fails, and the day's cached response answers instead of an error.
    assert failing.call_count == 1
    assert text != "commands.gwx.error_fetching"
    # Same request key for Paris coordinates rounded to ~100 m.
    assert cmd.db_manager.cached_count() == 1


def test_http_error_also_uses_the_cache():
    cmd = _gwx()
    cmd.db_manager = FakeDB()
    _call(cmd, Mock(return_value=_response(_payload())), *PARIS)
    cmd.db_manager.age_all(OPEN_METEO_CACHE_FRESH_HOURS + 1)
    text = _call(cmd, Mock(return_value=_response({}, ok=False)), *PARIS)
    assert text != "commands.gwx.error_fetching"


def test_error_without_any_cache_still_reports_failure():
    cmd = _gwx()
    cmd.db_manager = FakeDB()
    text = _call(cmd, Mock(side_effect=requests.exceptions.ConnectionError("boom")), *PARIS)
    assert text == "commands.gwx.error_fetching"


def test_expired_fresh_window_fetches_again():
    cmd = _gwx()
    cmd.db_manager = FakeDB()
    _call(cmd, Mock(return_value=_response(_payload())), *PARIS)
    cmd.db_manager.age_all(OPEN_METEO_CACHE_FRESH_HOURS + 1)
    get = Mock(return_value=_response(_payload()))
    text = _call(cmd, get, *PARIS)
    assert get.call_count == 1
    assert text != "commands.gwx.error_fetching"


def test_hourly_cache_has_the_short_window():
    cmd = _gwx()
    cmd.db_manager = FakeDB()
    _call(cmd, Mock(return_value=_response(_payload())), *PARIS, forecast_type="hourly")
    get = Mock(return_value=_response(_payload()))
    _call(cmd, get, *PARIS, forecast_type="hourly")
    assert get.call_count == 0  # Under the 1 h hourly window
    cmd.db_manager.age_all(2)
    get = Mock(return_value=_response(_payload()))
    _call(cmd, get, *PARIS, forecast_type="hourly")
    assert get.call_count == 1  # 2 h old hourly data is too stale to reuse


def test_paris_request_uses_arome_model():
    cmd = _gwx()
    cmd.db_manager = FakeDB()
    get = Mock(return_value=_response(_payload()))
    _call(cmd, get, *PARIS)
    assert get.call_args.kwargs['params']['models'] == OPEN_METEO_FRANCE_MODEL


def test_multiday_paris_keeps_best_match():
    cmd = _gwx()
    cmd.db_manager = FakeDB()
    get = Mock(return_value=_response(_payload()))
    _call(cmd, get, *PARIS, forecast_type="multiday", num_days=7)
    assert get.call_args.kwargs['params']['models'] == 'best_match'


def test_configured_model_wins_over_france_selection():
    cmd = _gwx({"weather_model": "ecmwf_ifs025"})
    cmd.db_manager = FakeDB()
    get = Mock(return_value=_response(_payload()))
    _call(cmd, get, *PARIS)
    assert get.call_args.kwargs['params']['models'] == 'ecmwf_ifs025'


def test_dom_location_with_fr_country_code_keeps_best_match():
    cmd = _gwx()
    cmd.db_manager = FakeDB()
    get = Mock(return_value=_response(_payload()))
    _call(cmd, get, *GUATELUPE, country_code='fr')
    assert get.call_args.kwargs['params']['models'] == 'best_match'


def test_cache_isolates_model_and_forecast_type_requests():
    cmd = _gwx()
    db = FakeDB()
    cmd.db_manager = db
    _call(cmd, Mock(return_value=_response(_payload())), *PARIS)
    get = Mock(return_value=_response(_payload()))
    _call(cmd, get, *PARIS, forecast_type="multiday", num_days=7)
    # Different forecast_days/model -> different key -> the API is asked again.
    assert get.call_count == 1
    assert db.cached_count() == 2


# ---------------------------------------------------------------------------
# Weather_Service daily forecast: model selection, cache reuse, failure fallback

def _service():
    config = configparser.ConfigParser()
    config.add_section("Weather")
    config.add_section("Weather_Service")
    config.set("Weather_Service", "my_position_lat", "48.8566")
    config.set("Weather_Service", "my_position_lon", "2.3522")
    config.set("Weather_Service", "weather_alarm", "06:00")

    bot = Mock()
    bot.logger = Mock()
    bot.config = config
    bot.db_manager = FakeDB()
    bot.translator.translate.side_effect = lambda key, **_kwargs: key
    service = WeatherService(bot)
    service._running = True
    return service


def _service_payload():
    offset = 3600
    local_now = datetime.now(timezone.utc) + timedelta(seconds=offset)
    return {
        'utc_offset_seconds': offset,
        'current': {'time': local_now.isoformat(timespec="minutes"), 'temperature_2m': 12.5,
                    'relative_humidity_2m': 78, 'apparent_temperature': 11.0, 'weather_code': 2,
                    'wind_speed_10m': 14.2, 'wind_direction_10m': 250, 'wind_gusts_10m': 30.1},
        'daily': {'time': [local_now.date().isoformat(), (local_now + timedelta(days=1)).date().isoformat()],
                  'weather_code': [2, 61], 'temperature_2m_max': [13.4, 11.0],
                  'temperature_2m_min': [6.2, 4.1], 'precipitation_probability_max': [20, 70],
                  'wind_speed_10m_max': [18.0, 22.0]},
    }


def _service_result(service, get_mock):
    service.api_session.get = get_mock
    with patch("modules.utils.rate_limited_nominatim_reverse", AsyncMock(return_value=None)):
        return asyncio.run(service._get_weather_forecast_result())


def test_daily_forecast_uses_arome_and_caches_it(mock_logger):
    service = _service()
    get = Mock(return_value=_response(_service_payload()))
    result = _service_result(service, get)
    assert result.retryable is False
    assert result.text != "services.weather_service.error_fetching"
    assert get.call_args.kwargs['params']['models'] == OPEN_METEO_FRANCE_MODEL
    assert service.bot.db_manager.cached_count() == 1

    # Same-day second run (e.g. a manual trigger after the alarm): answered from cache.
    get2 = Mock()
    second = _service_result(service, get2)
    assert get2.call_count == 0
    assert second.text == result.text


def test_daily_forecast_failure_falls_back_to_cache(mock_logger):
    service = _service()
    _service_result(service, Mock(return_value=_response(_service_payload())))
    service.bot.db_manager.age_all(OPEN_METEO_CACHE_FRESH_HOURS + 1)
    failing = Mock(return_value=_response({}, ok=False))
    result = _service_result(service, failing)
    # The API was asked again, failed, and the day's cached forecast answers
    # instead of a retryable error.
    assert failing.call_count == 1
    assert result.text != "services.weather_service.error_fetching"
    assert result.retryable is False


def test_daily_forecast_without_cache_still_reports_failure(mock_logger):
    service = _service()
    result = _service_result(service, Mock(return_value=_response({}, ok=False)))
    assert result.text == "services.weather_service.error_fetching"
    assert result.retryable is True
