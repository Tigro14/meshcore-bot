"""The scheduled Daily Weather message renders in French once the catalog defines it.

fr/fr-CA used to hold no services.weather_service section at all, so the
plugin's labels and WMO descriptions fell back to English even with
[Localization] language = fr. The daily wind also read "N6km/h"; it now
carries a space, as gwx does.
"""

import asyncio
import configparser
from unittest.mock import AsyncMock, Mock, patch

import pytest

from modules.i18n import Translator
from modules.service_plugins.weather_service import WeatherService

pytestmark = pytest.mark.unit


def build_service(*, language="fr", weather_overrides=None):
    cfg = configparser.ConfigParser()
    cfg.add_section("Weather")
    cfg.add_section("Weather_Service")
    cfg.set("Weather_Service", "my_position_lat", "48.0")
    cfg.set("Weather_Service", "my_position_lon", "2.0")
    cfg.set("Weather", "temperature_unit", "celsius")
    cfg.set("Weather", "wind_speed_unit", "kmh")
    for key, value in (weather_overrides or {}).items():
        cfg.set("Weather", key, value)

    bot = Mock()
    bot.logger = Mock()
    bot.config = cfg
    bot.db_manager = Mock()
    bot.translator = Translator(language, "translations/")

    service = WeatherService(bot)
    service.api_session = Mock()
    return service


def _payload():
    """Current and tomorrow fields for a cold clear morning in Rebréchien."""
    return {
        "current": {"temperature_2m": 2.4, "weather_code": 0,
                    "wind_speed_10m": 6.1, "wind_direction_10m": 0},
        "daily": {"time": ["2026-11-13", "2026-11-14"],
                  "weather_code": [0, 3],
                  "temperature_2m_max": [9.2, 9.4],
                  "temperature_2m_min": [1.1, 2.2]},
    }


def _forecast_text(service):
    response = Mock(ok=True)
    response.json.return_value = _payload()
    service.api_session.get = Mock(return_value=response)
    with patch.object(service, "_forecast_location_name",
                      new=AsyncMock(return_value="Rebréchien")):
        result = asyncio.run(service._get_weather_forecast_result())
    return result.text


def test_daily_forecast_renders_in_french():
    text = _forecast_text(build_service())
    assert "Rebréchien: ☀️Dégagé 2°C" in text
    assert "| Demain: ☁️Couvert" in text
    assert "Clear" not in text and "Tomorrow" not in text


def test_daily_wind_carries_a_spaced_unit():
    assert "N6 km/h" in _forecast_text(build_service())


def test_daily_weather_label_is_translated():
    assert build_service().bot.translator.translate(
        "services.weather_service.daily_weather") == "Météo du jour"


@pytest.mark.parametrize(("code", "want"), [
    (0, "Dégagé"), (3, "Couvert"), (61, "Pluie légère"), (95, "Orage"),
    (99, "Orage violent"),
])
def test_wmo_descriptions_are_translated(code, want):
    assert build_service()._get_weather_description(code) == want


def test_unknown_code_uses_the_french_fallback():
    assert build_service()._get_weather_description(4) == "Inconnu"


@pytest.mark.parametrize("language", ["fr", "fr-CA"])
def test_rain_alert_templates_render_in_french(language):
    catalog = Translator(language, "translations/")
    ptype = catalog.translate("commands.rain.precip_types.heavy_rain")
    starting = catalog.translate(
        "services.weather_service.rain_starting",
        emoji="🌧️", ptype=ptype, minutes=12, est=" (env. 8 mm, 80%)",
        steady=" (soutenue)", temp="", location=" près de Orléans")
    assert starting == "🌧️ Pluie forte commence dans ~12 min (env. 8 mm, 80%) (soutenue) près de Orléans"
    ending = catalog.translate(
        "services.weather_service.rain_ending",
        emoji="🌧️", ptype=ptype, minutes=18, est="", temp="", location="")
    assert ending == "🌧️ Pluie forte se termine dans ~18 min"


def test_english_fallback_is_untouched():
    text = _forecast_text(build_service(language="en"))
    assert "☀️Clear 2°C N6 km/h" in text
    assert "| Demain" not in text and "| Tomorrow:" in text
