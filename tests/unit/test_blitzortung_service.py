#!/usr/bin/env python3
"""Unit tests for BlitzortungService."""

import asyncio
import configparser
from unittest.mock import AsyncMock, Mock, patch

import pytest

from modules.service_plugins.blitzortung_service import BlitzortungService

# ── helpers ────────────────────────────────────────────────────────────────────


def _build_bot(config: configparser.ConfigParser) -> Mock:
    bot = Mock()
    bot.logger = Mock()
    bot.config = config
    bot.command_manager = Mock()
    bot.command_manager.send_channel_message = AsyncMock()
    return bot


def _base_config(
    *,
    threshold: int = 10,
    window: int = 10,
    channel: str = "#bot",
    lat: str = "47.5",
    lon: str = "15.6",
    add_area: bool = True,
) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.add_section("Blitzortung_Service")
    cfg.set("Blitzortung_Service", "enabled", "true")
    cfg.set("Blitzortung_Service", "channel", channel)
    cfg.set("Blitzortung_Service", "alert_threshold", str(threshold))
    cfg.set("Blitzortung_Service", "window_minutes", str(window))
    cfg.set("Blitzortung_Service", "my_position_lat", lat)
    cfg.set("Blitzortung_Service", "my_position_lon", lon)
    if add_area:
        cfg.set("Blitzortung_Service", "blitz_area_min_lat", "47.0")
        cfg.set("Blitzortung_Service", "blitz_area_min_lon", "15.0")
        cfg.set("Blitzortung_Service", "blitz_area_max_lat", "48.0")
        cfg.set("Blitzortung_Service", "blitz_area_max_lon", "16.5")
    return cfg


def _make_service(cfg: configparser.ConfigParser) -> BlitzortungService:
    with patch(
        "modules.service_plugins.blitzortung_service.MQTT_AVAILABLE", True
    ):
        return BlitzortungService(_build_bot(cfg))


# ── config reading tests ───────────────────────────────────────────────────────


def test_config_channel_and_threshold():
    svc = _make_service(_base_config(threshold=15, channel="#storm"))
    assert svc.channel == "#storm"
    assert svc.alert_threshold == 15
    assert svc.enabled


def test_config_window_minutes_converted_to_seconds():
    svc = _make_service(_base_config(window=15))
    assert svc.window_seconds == 15 * 60.0


def test_config_defaults():
    cfg = _base_config()
    # Remove threshold and window so defaults kick in
    cfg.remove_option("Blitzortung_Service", "alert_threshold")
    cfg.remove_option("Blitzortung_Service", "window_minutes")
    svc = _make_service(cfg)
    assert svc.alert_threshold == 10
    assert svc.window_seconds == 600.0


def test_position_fallback_from_weather_service():
    cfg = _base_config()
    cfg.remove_option("Blitzortung_Service", "my_position_lat")
    cfg.remove_option("Blitzortung_Service", "my_position_lon")
    cfg.add_section("Weather_Service")
    cfg.set("Weather_Service", "my_position_lat", "48.0")
    cfg.set("Weather_Service", "my_position_lon", "16.0")
    svc = _make_service(cfg)
    assert svc.my_position_lat == pytest.approx(48.0)
    assert svc.my_position_lon == pytest.approx(16.0)
    assert svc.enabled


def test_disabled_when_no_position():
    cfg = _base_config()
    cfg.remove_option("Blitzortung_Service", "my_position_lat")
    cfg.remove_option("Blitzortung_Service", "my_position_lon")
    svc = _make_service(cfg)
    assert not svc.enabled


def test_disabled_when_no_bounding_box():
    cfg = _base_config(add_area=False)
    svc = _make_service(cfg)
    assert not svc.enabled


def test_disabled_when_mqtt_not_available():
    cfg = _base_config()
    with patch("modules.service_plugins.blitzortung_service.MQTT_AVAILABLE", False):
        svc = BlitzortungService(_build_bot(cfg))
    assert not svc.enabled


# ── geometry helpers ───────────────────────────────────────────────────────────


def test_heading_to_compass_north():
    svc = _make_service(_base_config())
    assert svc._heading_to_compass(0) == "N"
    assert svc._heading_to_compass(360) == "N"


def test_heading_to_compass_cardinal():
    svc = _make_service(_base_config())
    assert svc._heading_to_compass(90) == "E"
    assert svc._heading_to_compass(180) == "S"
    assert svc._heading_to_compass(270) == "W"


def test_heading_to_compass_intercardinal():
    svc = _make_service(_base_config())
    assert svc._heading_to_compass(45) == "NE"
    assert svc._heading_to_compass(315) == "NW"


def test_calculate_heading_and_distance_north():
    svc = _make_service(_base_config())
    heading, dist = svc._calculate_heading_and_distance(47.5, 15.6, 48.5, 15.6)
    # Strike is due north — heading should be close to 0 (or 360)
    assert heading == 0 or heading == 360 or abs(heading - 0) < 2 or abs(heading - 360) < 2
    assert dist == pytest.approx(111.0, rel=0.05)  # ~111 km per degree of latitude


def test_calculate_heading_and_distance_east():
    svc = _make_service(_base_config())
    heading, dist = svc._calculate_heading_and_distance(47.5, 15.6, 47.5, 16.6)
    assert heading == pytest.approx(90, abs=2)
    assert dist > 0


# ── buffer processing ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_empty_buffer_does_nothing():
    svc = _make_service(_base_config())
    svc.blitz_buffer = []
    await svc._process_lightning_buffer()
    svc.bot.command_manager.send_channel_message.assert_not_called()


@pytest.mark.asyncio
async def test_process_below_threshold_no_alert():
    svc = _make_service(_base_config(threshold=10))
    # 9 strikes — below threshold
    svc.blitz_buffer = [
        {"key": "90|5", "heading": 90, "distance": 50.0, "lat": 47.5, "lon": 16.1, "timestamp": 0}
    ] * 9
    await svc._process_lightning_buffer()
    svc.bot.command_manager.send_channel_message.assert_not_called()


@pytest.mark.asyncio
async def test_process_meets_threshold_sends_alert():
    svc = _make_service(_base_config(threshold=5))
    svc.blitz_buffer = [
        {"key": "90|5", "heading": 90, "distance": 50.0, "lat": 47.5, "lon": 16.1, "timestamp": 0}
    ] * 5

    with patch.object(svc, "_geocode_location", AsyncMock(return_value="Testtown")):
        await svc._process_lightning_buffer()

    svc.bot.command_manager.send_channel_message.assert_called_once()
    call_args = svc.bot.command_manager.send_channel_message.call_args
    assert "Testtown" in call_args[0][1]
    assert "🌩️" in call_args[0][1]


@pytest.mark.asyncio
async def test_process_buffer_reset_after_run():
    svc = _make_service(_base_config(threshold=3))
    svc.blitz_buffer = [
        {"key": "90|5", "heading": 90, "distance": 50.0, "lat": 47.5, "lon": 16.1, "timestamp": 0}
    ] * 3

    with patch.object(svc, "_geocode_location", AsyncMock(return_value=None)):
        await svc._process_lightning_buffer()

    assert svc.blitz_buffer == []
    assert svc.seen_blitz_keys == set()


@pytest.mark.asyncio
async def test_process_no_duplicate_alert_within_cycle():
    """A bucket already seen in the current cycle must not fire twice."""
    svc = _make_service(_base_config(threshold=3))
    svc.blitz_buffer = [
        {"key": "90|5", "heading": 90, "distance": 50.0, "lat": 47.5, "lon": 16.1, "timestamp": 0}
    ] * 6
    svc.seen_blitz_keys = {"90|5"}  # already alerted

    with patch.object(svc, "_geocode_location", AsyncMock(return_value=None)):
        await svc._process_lightning_buffer()

    svc.bot.command_manager.send_channel_message.assert_not_called()


@pytest.mark.asyncio
async def test_process_silence_mesh_output_skips_channel_message():
    svc = _make_service(_base_config(threshold=3))
    svc.silence_mesh_output = True
    svc.blitz_buffer = [
        {"key": "90|5", "heading": 90, "distance": 50.0, "lat": 47.5, "lon": 16.1, "timestamp": 0}
    ] * 3

    with (
        patch.object(svc, "_geocode_location", AsyncMock(return_value=None)),
        patch.object(svc, "send_external_notifications", AsyncMock()) as mock_ext,
    ):
        await svc._process_lightning_buffer()

    svc.bot.command_manager.send_channel_message.assert_not_called()
    mock_ext.assert_called_once()


# ── Weather_Service deconfliction ──────────────────────────────────────────────


def test_weather_service_skips_blitz_when_blitzortung_service_enabled(mock_logger):
    """WeatherService must not start blitz tasks when Blitzortung_Service is enabled."""
    from modules.service_plugins.weather_service import WeatherService

    cfg = configparser.ConfigParser()
    cfg.add_section("Weather_Service")
    cfg.set("Weather_Service", "my_position_lat", "47.5")
    cfg.set("Weather_Service", "my_position_lon", "15.6")
    cfg.set("Weather_Service", "blitz_area_min_lat", "47.0")
    cfg.set("Weather_Service", "blitz_area_min_lon", "15.0")
    cfg.set("Weather_Service", "blitz_area_max_lat", "48.0")
    cfg.set("Weather_Service", "blitz_area_max_lon", "16.5")
    cfg.add_section("Blitzortung_Service")
    cfg.set("Blitzortung_Service", "enabled", "true")
    cfg.add_section("Weather")

    bot = _build_bot(cfg)
    bot.logger = mock_logger

    svc = WeatherService(bot)

    created_tasks: list[str] = []
    original_create_task = asyncio.create_task

    def spy_create_task(coro, **kwargs):
        created_tasks.append(coro.__name__ if hasattr(coro, "__name__") else str(coro))
        # Cancel immediately so we don't need a running loop
        t = original_create_task(coro, **kwargs)
        t.cancel()
        return t

    async def run_start():
        with patch("asyncio.create_task", side_effect=spy_create_task):
            await svc.start()

    asyncio.run(run_start())

    lightning_tasks = [t for t in created_tasks if "lightning" in t or "blitzortung" in t.lower()]
    assert lightning_tasks == [], (
        f"Expected no lightning tasks but got: {lightning_tasks}"
    )
