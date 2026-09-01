from __future__ import annotations

import re
from configparser import ConfigParser
from datetime import datetime
from unittest.mock import Mock

import pytest
import requests

from modules import solar_conditions as solar

SOLAR_XML = """\
<solar>
  <solardata>
    <solarflux>143</solarflux>
    <aindex> 9</aindex>
    <kindex> 2</kindex>
    <xray>C1.0</xray>
    <sunspots>102</sunspots>
    <signalnoise>S1-S2</signalnoise>
    <calculatedconditions>
      <band name="80m-40m" time="day">Fair</band>
      <band name="30m-20m" time="day">Good</band>
      <band name="17m-15m" time="day">Good</band>
      <band name="12m-10m" time="day">Fair</band>
      <band name="80m-40m" time="night">Good</band>
      <band name="30m-20m" time="night">Good</band>
      <band name="17m-15m" time="night">Good</band>
      <band name="12m-10m" time="night">Poor</band>
    </calculatedconditions>
  </solardata>
</solar>
"""


class FakeResponse:
    def __init__(self, *, text: str = "", payload: dict | None = None, error: Exception | None = None):
        self.text = text
        self._payload = payload or {}
        self._error = error

    def raise_for_status(self) -> None:
        if self._error:
            raise self._error

    def json(self) -> dict:
        return self._payload


@pytest.fixture(autouse=True)
def reset_config():
    solar.set_config(None)
    yield
    solar.set_config(None)


def _config(*, zulu: bool = False, api_key: str = "") -> ConfigParser:
    config = ConfigParser()
    config["Bot"] = {"bot_latitude": "47.6", "bot_longitude": "-122.3"}
    config["Solar_Config"] = {"url_timeout": "7", "use_zulu_time": str(zulu)}
    config["External_Data"] = {"n2yo_api_key": api_key}
    return config


def test_hamqsl_formats_are_compatible(monkeypatch):
    monkeypatch.setattr(
        solar.requests,
        "get",
        lambda url, timeout: FakeResponse(text=SOLAR_XML),
    )

    assert solar.hf_band_conditions() == (
        "d80m-40m=Fair\n"
        "d30m-20m=Good\n"
        "d17m-15m=Good\n"
        "d12m-10m=Fair\n"
        "n80m-40m=Good\n"
        "n30m-20m=Good\n"
        "n17m-15m=Good\n"
        "n12m-10m=Poor"
    )
    assert solar.solar_conditions() == (
        "A-Index:  9\n"
        "K-Index:  2\n"
        "Sunspots: 102\n"
        "X-Ray Flux: C1.0\n"
        "Solar Flux: 143\n"
        "Signal Noise: S1-S2"
    )
    assert solar.solar_conditions_condensed() == "A: 9 K: 2 Sun:102 Flux:143 Xray:C1.0 Noise:S1-S2"
    assert solar.hf_band_conditions_condensed() == (
        "D:80m-40m-12m-10mFair 30m-20m-17m-15mGood "
        "N:80m-40m-17m-15mGood 12m-10mPoor"
    )


def test_hamqsl_failure_returns_existing_error_contract(monkeypatch):
    error = requests.HTTPError("unavailable")
    monkeypatch.setattr(
        solar.requests,
        "get",
        lambda url, timeout: FakeResponse(error=error),
    )

    assert solar.hf_band_conditions() == solar.ERROR_FETCHING_DATA
    assert solar.solar_conditions() == solar.ERROR_FETCHING_DATA


def test_drap_extracts_only_the_xray_message(monkeypatch):
    report = """\
# Product: D-Region Absorption
#  X-RAY Message : M-class flare absorption possible
#  Proton Message : Normal Proton Background
"""
    monkeypatch.setattr(
        solar.requests,
        "get",
        lambda url, timeout: FakeResponse(text=report),
    )

    assert solar.drap_xray_conditions() == "M-class flare absorption possible"


def test_satellite_pass_uses_documented_n2yo_schema(monkeypatch):
    solar.set_config(_config(api_key="secret"))
    calls = []
    payload = {
        "info": {"satname": "SPACE STATION", "passescount": 1},
        "passes": [
            {
                "startUTC": 1893456000,
                "startAzCompass": "NW",
                "maxEl": 52.19,
                "endUTC": 1893456630,
                "endAzCompass": "ESE",
            }
        ],
    }

    def fake_get(url, timeout):
        calls.append((url, timeout))
        return FakeResponse(payload=payload)

    monkeypatch.setattr(solar.requests, "get", fake_get)
    monkeypatch.setattr(solar, "time", lambda: 1893450000)

    result = solar.get_next_satellite_pass("25544")

    assert "/radiopasses/25544/47.6/-122.3/0/10/0/" in calls[0][0]
    assert calls[0][1] == solar.DEFAULT_URL_TIMEOUT
    start_text = datetime.fromtimestamp(1893456000).strftime("%a %d %I:%M%p")
    end_text = datetime.fromtimestamp(1893456630).strftime("%a %d %I:%M%p")
    assert result == (
        f"SPACE STATION @{start_text} Az:NW for10m30s, "
        f"MaxEl:52.19° Set@{end_text} Az:ESE"
    )


def test_satellite_visual_pass_uses_duration_and_visual_endpoint(monkeypatch):
    solar.set_config(_config(api_key="secret"))
    calls = []
    payload = {
        "info": {"satname": "SPACE STATION", "passescount": 1},
        "passes": [
            {
                "startUTC": 1893456000,
                "startAzCompass": "NW",
                "maxEl": 78.27,
                "endUTC": 1893456630,
                "endAzCompass": "SE",
                "duration": 485,
            }
        ],
    }

    def fake_get(url, timeout):
        calls.append(url)
        return FakeResponse(payload=payload)

    monkeypatch.setattr(solar.requests, "get", fake_get)
    monkeypatch.setattr(solar, "time", lambda: 1893450000)

    result = solar.get_next_satellite_pass("25544", use_visual=True)

    assert "/visualpasses/25544/47.6/-122.3/0/10/60/" in calls[0]
    assert "for8m5s" in result


def test_satellite_pass_skips_passes_that_have_already_ended(monkeypatch):
    solar.set_config(_config(api_key="secret"))
    payload = {
        "info": {"satname": "SPACE STATION"},
        "passes": [
            {
                "startUTC": 3000,
                "startAzCompass": "W",
                "maxEl": 12,
                "endUTC": 3060,
                "endAzCompass": "E",
            },
            {
                "startUTC": 5000,
                "startAzCompass": "NW",
                "maxEl": 52,
                "endUTC": 5045,
                "endAzCompass": "SE",
            },
        ],
    }
    monkeypatch.setattr(solar.requests, "get", lambda url, timeout: FakeResponse(payload=payload))
    monkeypatch.setattr(solar, "time", lambda: 4000)
    monkeypatch.setattr(solar, "_unix_local", lambda timestamp: f"T{timestamp}")

    result = solar.get_next_satellite_pass("25544")

    assert result == "SPACE STATION @T5000 Az:NW for45s, MaxEl:52° Set@T5045 Az:SE"


def test_satellite_pass_includes_pass_starting_at_current_time(monkeypatch):
    solar.set_config(_config(api_key="secret"))
    payload = {
        "info": {"satname": "SPACE STATION"},
        "passes": [
            {
                "startUTC": 5000,
                "startAzCompass": "NW",
                "maxEl": 52,
                "endUTC": 5045,
                "endAzCompass": "SE",
            }
        ],
    }
    monkeypatch.setattr(solar.requests, "get", lambda url, timeout: FakeResponse(payload=payload))
    monkeypatch.setattr(solar, "time", lambda: 5000)
    monkeypatch.setattr(solar, "_unix_local", lambda timestamp: f"T{timestamp}")

    result = solar.get_next_satellite_pass("25544")

    assert result == "SPACE STATION @T5000 Az:NW for45s, MaxEl:52° Set@T5045 Az:SE"


def test_satellite_pass_rejects_geo_duration(monkeypatch):
    solar.set_config(_config(api_key="secret"))
    payload = {
        "info": {"satname": "GOES-18"},
        "passes": [
            {
                "startUTC": 5000,
                "startAzCompass": "S",
                "maxEl": 35,
                "endUTC": 12201,
                "endAzCompass": "S",
                "duration": 7201,
            }
        ],
    }
    monkeypatch.setattr(solar.requests, "get", lambda url, timeout: FakeResponse(payload=payload))
    monkeypatch.setattr(solar, "time", lambda: 4000)

    assert solar.get_next_satellite_pass("51850") == "GOES-18: GEO (no LEO pass)"


def test_satellite_time_format_honors_zulu_setting():
    timestamp = 1893456000

    solar.set_config(_config(zulu=False))
    assert solar._unix_local(timestamp) == datetime.fromtimestamp(timestamp).strftime("%a %d %I:%M%p")

    solar.set_config(_config(zulu=True))
    assert solar._unix_local(timestamp) == datetime.fromtimestamp(timestamp).strftime("%a %d %H:%M")


def test_satellite_pass_requires_an_api_key():
    solar.set_config(_config())
    assert solar.get_next_satellite_pass("25544") == "not configured, bug your sysop"


@pytest.mark.parametrize("satellite", ["not-a-number", "", "0", "-1"])
def test_satellite_pass_rejects_invalid_norad_without_http(monkeypatch, satellite):
    solar.set_config(_config(api_key="secret"))
    request = Mock()
    monkeypatch.setattr(solar.requests, "get", request)

    assert solar.get_next_satellite_pass(satellite) == "Provide NORAD# example use:🛰️satpass 25544,33591"
    request.assert_not_called()


def test_sun_and_moon_outputs_match_command_parser_contract():
    solar.set_config(_config())

    sun = solar.get_sun()
    moon = solar.get_moon()

    daytime = re.search(r"^SunSet: \w{3} \d{2} \d{2}:\d{2}(?:AM|PM)$", sun, re.MULTILINE) and re.search(
        r"^Rise: \w{3} \d{2} \d{2}:\d{2}(?:AM|PM)$", sun, re.MULTILINE
    )
    nighttime = re.search(r"^SunRise: \w{3} \d{2} \d{2}:\d{2}(?:AM|PM)$", sun, re.MULTILINE) and re.search(
        r"^Set: \w{3} \d{2} \d{2}:\d{2}(?:AM|PM)$", sun, re.MULTILINE
    )
    assert daytime or nighttime
    assert "Daylight: " in sun
    assert "Azimuth: " in sun
    assert re.search(r"^MoonRise:\w{3} \d{2} \d{2}:\d{2}(?:AM|PM)$", moon, re.MULTILINE)
    assert re.search(r"^Phase:(?:New Moon|Waxing|First Quarter|Full Moon|Waning|Last Quarter)", moon, re.MULTILINE)
    assert "FullMoon:" in moon
    assert "NewMoon:" in moon


def test_sun_at_night_lists_rise_then_set_without_daylight_remaining_or_negative_altitude(monkeypatch):
    class FakeSun:
        alt = -0.1
        az = 1.0

        def __init__(self, observer):
            pass

        def compute(self, observer):
            pass

    class FakeObserver:
        date = 10.0

        def next_rising(self, body):
            return 10.25

        def next_setting(self, body, start=None):
            return 10.75

        def previous_rising(self, body):
            return 9.25

    monkeypatch.setattr(solar, "_observer", lambda lat, lon: FakeObserver())
    monkeypatch.setattr(solar.ephem, "Sun", FakeSun)
    monkeypatch.setattr(solar, "_format_event", lambda value: {10.25: "RISE", 10.75: "SET"}[value])

    result = solar.get_sun(47.6, -122.3)

    assert result.splitlines() == [
        "SunRise: RISE",
        "Set: SET",
        "Daylight: 12h 0m",
        f"Azimuth: {180.0 / solar.ephem.pi:.2f}°",
    ]
    assert "Remaining:" not in result
    assert "Altitude:" not in result


def test_typed_config_values_and_invalid_fallback(caplog):
    config = ConfigParser()
    config["Solar_Config"] = {
        "url_timeout": "23",
        "use_zulu_time": "yes",
        "bad_number": "not-a-number",
    }
    solar.set_config(config)

    assert solar.get_config_value("Solar_Config", "url_timeout", 10) == 23
    assert solar.get_config_value("Solar_Config", "use_zulu_time", False) is True
    assert solar.get_config_value("Solar_Config", "bad_number", 5) == 5
