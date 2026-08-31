"""Regression tests for blocking providers reached by the async wx command."""

import asyncio
import threading
from typing import Any

import pytest

from modules.commands.wx_command import WxCommand

_BOUNDARIES = [
    (
        "wxsim",
        "_get_wxsim_weather_async",
        "_get_wxsim_weather",
        ("https://weather.example/plaintext.txt",),
        {},
    ),
    (
        "reverse_geocode",
        "_coordinates_to_location_string_async",
        "_coordinates_to_location_string",
        (47.6, -122.3),
        {},
    ),
    (
        "zipcode_geocode",
        "_zipcode_to_lat_lon_async",
        "zipcode_to_lat_lon",
        ("98101",),
        {},
    ),
    (
        "city_geocode",
        "_city_to_lat_lon_async",
        "city_to_lat_lon",
        ("Seattle",),
        {},
    ),
    (
        "forecast_workflow",
        "get_weather_for_location",
        "_get_weather_for_location_sync",
        ("Seattle", "city"),
        {},
    ),
    (
        "alert_fetch_and_parse",
        "_get_weather_alerts_noaa_async",
        "get_weather_alerts_noaa",
        (47.6, -122.3),
        {"return_full_data": True},
    ),
]


def _command() -> WxCommand:
    command = object.__new__(WxCommand)
    command._sync_provider_lock = threading.Lock()
    return command


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("_name", "async_name", "sync_name", "args", "kwargs"),
    _BOUNDARIES,
    ids=[case[0] for case in _BOUNDARIES],
)
async def test_wx_provider_boundaries_keep_event_loop_responsive(
    monkeypatch: pytest.MonkeyPatch,
    _name: str,
    async_name: str,
    sync_name: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> None:
    command = _command()
    provider_started = threading.Event()
    heartbeat_ran = threading.Event()
    provider_saw_heartbeat = False
    expected = object()

    def slow_provider(*_args: Any, **_kwargs: Any) -> object:
        nonlocal provider_saw_heartbeat
        provider_started.set()
        provider_saw_heartbeat = heartbeat_ran.wait(timeout=1)
        return expected

    monkeypatch.setattr(command, sync_name, slow_provider)

    async def heartbeat() -> None:
        while not provider_started.is_set():
            await asyncio.sleep(0)
        heartbeat_ran.set()

    heartbeat_task = asyncio.create_task(heartbeat())
    operation = getattr(command, async_name)
    result = await asyncio.wait_for(operation(*args, **kwargs), timeout=2)
    await heartbeat_task

    assert result is expected
    assert provider_saw_heartbeat, "provider work blocked the asyncio event loop"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("_name", "async_name", "sync_name", "args", "kwargs"),
    _BOUNDARIES,
    ids=[case[0] for case in _BOUNDARIES],
)
async def test_wx_provider_boundary_awaits_are_cancellable(
    monkeypatch: pytest.MonkeyPatch,
    _name: str,
    async_name: str,
    sync_name: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> None:
    command = _command()
    provider_started = threading.Event()
    release_provider = threading.Event()
    provider_finished = threading.Event()

    def slow_provider(*_args: Any, **_kwargs: Any) -> object:
        provider_started.set()
        try:
            release_provider.wait(timeout=1)
            return object()
        finally:
            provider_finished.set()

    monkeypatch.setattr(command, sync_name, slow_provider)
    operation = getattr(command, async_name)
    task = asyncio.create_task(operation(*args, **kwargs))

    try:
        assert await asyncio.to_thread(provider_started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release_provider.set()
        assert await asyncio.to_thread(provider_finished.wait, 1)


@pytest.mark.asyncio
async def test_wx_provider_lock_wait_does_not_block_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = _command()
    command._sync_provider_lock.acquire()
    heartbeat_ran = False

    monkeypatch.setattr(command, "get_weather_alerts_noaa", lambda *_args: "done")

    async def release_after_heartbeat() -> None:
        nonlocal heartbeat_ran
        await asyncio.sleep(0)
        heartbeat_ran = True
        command._sync_provider_lock.release()

    heartbeat_task = asyncio.create_task(release_after_heartbeat())
    result = await asyncio.wait_for(
        command._get_weather_alerts_noaa_async(47.6, -122.3), timeout=2
    )
    await heartbeat_task

    assert result == "done"
    assert heartbeat_ran


@pytest.mark.asyncio
async def test_wx_shared_session_work_remains_serialized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = _command()
    first_provider_started = threading.Event()
    release_providers = threading.Event()
    counter_lock = threading.Lock()
    active = 0
    max_active = 0

    def slow_alert_fetch(*_args: Any, **_kwargs: Any) -> str:
        nonlocal active, max_active
        with counter_lock:
            active += 1
            max_active = max(max_active, active)
        first_provider_started.set()
        release_providers.wait(timeout=1)
        with counter_lock:
            active -= 1
        return "done"

    monkeypatch.setattr(command, "get_weather_alerts_noaa", slow_alert_fetch)
    first = asyncio.create_task(command._get_weather_alerts_noaa_async(47.6, -122.3))
    assert await asyncio.to_thread(first_provider_started.wait, 1)
    second = asyncio.create_task(command._get_weather_alerts_noaa_async(48.0, -123.0))

    await asyncio.sleep(0.05)
    release_providers.set()
    assert await asyncio.gather(first, second) == ["done", "done"]
    assert max_active == 1
