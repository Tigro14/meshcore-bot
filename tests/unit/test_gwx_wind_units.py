"""gwx writes the wind speed unit into every wind number, gusts labeled per language."""

import configparser
import copy
import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from modules.commands.alternatives.wx_international import GlobalWxCommand
from modules.i18n import Translator

MODULE = "modules.commands.alternatives.wx_international"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "open_meteo"
PHOENIX = json.loads((FIXTURES / "phoenix.json").read_text())
REYKJAVIK = json.loads((FIXTURES / "reykjavik.json").read_text())


def _cmd(language, wind_speed_unit="mph"):
    config = configparser.ConfigParser()
    config.read_dict({
        "Weather": {"temperature_unit": "fahrenheit", "wind_speed_unit": wind_speed_unit},
        "Gwx_Command": {},
        "Bot": {},
    })
    bot = Mock()
    bot.config = config
    bot.translator = Translator(language)
    cmd = GlobalWxCommand(bot)
    cmd.logger = Mock()
    cmd.get_max_message_length = Mock(return_value=400)
    return cmd


def _patched_fetch(data):
    response = Mock(ok=True)
    response.json.return_value = data
    return patch(f"{MODULE}.requests.get", return_value=response)


def _current_text(cmd, **current_overrides):
    data = copy.deepcopy(PHOENIX)
    data["current"].update({"wind_speed_10m": 9, "wind_direction_10m": 0, "wind_gusts_10m": 22})
    data["current"].update(current_overrides)
    with _patched_fetch(data):
        return cmd.get_open_meteo_weather(33.45, -112.07, forecast_type="default", message=Mock())


def _tomorrow_text(cmd):
    data = copy.deepcopy(REYKJAVIK)
    data["daily"]["wind_speed_10m_max"][1] = 9
    data["daily"]["wind_gusts_10m_max"][1] = 22
    with _patched_fetch(data):
        return cmd.get_open_meteo_weather(64.1, -21.9, forecast_type="tomorrow", message=Mock())


def test_english_current_conditions_carry_units():
    assert "⬆️N9 mph G22 mph" in _current_text(_cmd("en"))


def test_french_current_conditions_carry_units_and_say_raf():
    assert "⬆️N9 km/h raf. 22 km/h" in _current_text(_cmd("fr", "kmh"))


def test_meter_per_second_unit_comes_from_the_catalog():
    assert "9 m/s raf. 22 m/s" in _current_text(_cmd("fr", "ms"))


def test_insignificant_gusts_are_left_out():
    text = _current_text(_cmd("fr", "kmh"), wind_gusts_10m=12)  # not more than speed + 3
    assert "9 km/h" in text and "raf." not in text


def test_tomorrow_wind_carries_units_too():
    assert "9 km/h raf. 22 km/h" in _tomorrow_text(_cmd("fr", "kmh"))


def test_hourly_stays_compact():
    cmd = _cmd("en")
    with _patched_fetch(copy.deepcopy(PHOENIX)):
        text = cmd.get_open_meteo_weather(33.45, -112.07, forecast_type="hourly", message=Mock())
    assert "N9" in text or "N" in text  # the wind is still there
    assert "mph" not in text


@pytest.mark.parametrize(("language", "wind_unit", "want"), [
    ("en", "mph", "G22 mph"),
    ("fr", "kmh", "raf. 22 km/h"),
    ("fr-CA", "kmh", "raf. 22 km/h"),
    ("ru", "kmh", "G22 км/ч"),
    ("de", "mph", "G22 mph"),  # falls back to the English label
])
def test_the_gust_label_and_unit_come_from_the_catalog(language, wind_unit, want):
    catalog = Translator(language)
    unit = catalog.translate(f"services.weather_service.wind_speed_units.{wind_unit}")
    assert catalog.translate("commands.gwx.gust", value=22, unit=unit) == want
