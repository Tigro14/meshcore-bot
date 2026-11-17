"""gwx names the place in its reply only when that adds information, as wx does."""

import asyncio
import configparser
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from modules.commands.alternatives.wx_international import GlobalWxCommand

MODULE = "modules.commands.alternatives.wx_international"


def _gwx(default_country="US", default_state="", always=None, default_city=None):
    weather = {"default_country": default_country, "default_state": default_state}
    if default_city is not None:
        weather["default_city"] = default_city
    if always is not None:
        weather["always_show_location"] = str(always).lower()
    config = configparser.ConfigParser()
    config.read_dict({
        "Weather": weather,
        "Gwx_Command": {},
        "Bot": {},
    })
    bot = Mock()
    bot.config = config
    bot.translator.translate = Mock(side_effect=lambda key, **kwargs: key)
    cmd = GlobalWxCommand(bot)
    cmd.get_open_meteo_weather = Mock(return_value="Clear 20°C")
    cmd._get_open_meteo_weather_with_conditions = Mock(return_value=("Clear 20°C", {}))
    return cmd


def _reply(cmd, location, address, default_location=False):
    cmd.geocode_location = Mock(return_value=(1.0, 2.0, address, None))
    return cmd._get_weather_for_location_sync(
        location, message=None, default_location=default_location
    )


PARIS = {"city": "Paris", "country_code": "fr"}
LONDON = {"city": "London", "country_code": "gb"}
SEATTLE = {"city": "Seattle", "state": "Washington", "country_code": "us"}
PORTLAND = {"city": "Portland", "state": "Oregon", "country_code": "us"}


@pytest.mark.parametrize(("default_country", "default_state", "location", "address", "expected"), [
    ("GB", "", "London", LONDON, "Clear 20°C"),
    ("GB", "", "Paris", PARIS, "Paris, FR: Clear 20°C"),
    ("US", "WA", "Seattle", SEATTLE, "Clear 20°C"),
    ("US", "Washington", "Seattle", SEATTLE, "Clear 20°C"),
    ("US", "WA", "Portland", PORTLAND, "Portland, OR: Clear 20°C"),
    ("US", "", "Seattle", SEATTLE, "Seattle, WA: Clear 20°C"),
    ("US", "WA", "Paris", PARIS, "Paris, FR: Clear 20°C"),
])
def test_cities_are_named_when_they_leave_the_default_region(default_country, default_state, location, address, expected):
    assert _reply(_gwx(default_country, default_state), location, address) == expected


@pytest.mark.parametrize(("address", "expected"), [
    (SEATTLE, "Seattle, WA: Clear 20°C"),
    ({"country_code": "us", "state": "Washington"}, "Clear 20°C"),  # no place name: no label
    ({}, "Clear 20°C"),  # the reverse lookup failed
])
def test_coordinates_are_named_when_a_place_is_found(address, expected):
    # Even inside the default state: the user did not type a place name.
    assert _reply(_gwx("US", "WA"), "47.60620,-122.33210", address) == expected


def test_a_zip_code_is_not_named_and_not_reverse_geocoded():
    cmd = _gwx()
    with patch(f"{MODULE}.geocode_zipcode_sync", return_value=(47.6, -122.3)), patch(
        f"{MODULE}.rate_limited_nominatim_reverse_sync"
    ) as reverse:
        assert cmd.geocode_location("98104") == (47.6, -122.3, {}, None)
    reverse.assert_not_called()
    assert _reply(cmd, "98104", {}) == "Clear 20°C"


def test_an_unnamed_reply_keeps_the_whole_budget():
    cmd = _gwx("GB")
    _reply(cmd, "London", LONDON)
    assert cmd._get_open_meteo_weather_with_conditions.call_args.kwargs["location_prefix_len"] == 0


@pytest.mark.parametrize(("location", "address", "expected"), [
    ("Seattle", SEATTLE, "Seattle, WA: Clear 20°C"),  # inside the default state
    ("London", LONDON, "London, GB: Clear 20°C"),
    ("98104", SEATTLE, "Seattle, WA: Clear 20°C"),
    ("98104", {}, "Clear 20°C"),  # the reverse lookup failed
    ("98104", {"country_code": "us", "state": "Washington"}, "Clear 20°C"),  # no place name
    ("47.60620,-122.33210", {"country_code": "us", "state": "Washington"}, "Clear 20°C"),
])
def test_always_show_location_names_every_place_found(location, address, expected):
    assert _reply(_gwx("US", "WA", always=True), location, address) == expected


@pytest.mark.parametrize("always", [None, False])
def test_always_show_location_is_off_by_default(always):
    assert _reply(_gwx("US", "WA", always=always), "Seattle", SEATTLE) == "Clear 20°C"


def test_always_show_location_reverse_geocodes_a_zip_code():
    cmd = _gwx("US", "WA", always=True)
    place = Mock(raw={"address": SEATTLE})
    with patch(f"{MODULE}.geocode_zipcode_sync", return_value=(47.6, -122.3)), patch(
        f"{MODULE}.rate_limited_nominatim_reverse_sync", return_value=place
    ) as reverse:
        assert cmd.geocode_location("98104") == (47.6, -122.3, SEATTLE, place)
    reverse.assert_called_once()


def test_always_show_location_survives_a_failed_zip_reverse_lookup():
    cmd = _gwx("US", "WA", always=True)
    with patch(f"{MODULE}.geocode_zipcode_sync", return_value=(47.6, -122.3)), patch(
        f"{MODULE}.rate_limited_nominatim_reverse_sync", side_effect=TimeoutError
    ):
        assert cmd.geocode_location("98104") == (47.6, -122.3, {}, None)


def test_a_named_place_comes_out_of_the_budget():
    cmd = _gwx("US", "WA", always=True)
    _reply(cmd, "Seattle", SEATTLE)
    assert cmd._get_open_meteo_weather_with_conditions.call_args.kwargs["location_prefix_len"] == len("Seattle, WA: ")


def test_a_bare_request_names_the_default_city():
    # A sender who typed no place cannot infer the bot's default from "Ce soir: …".
    cmd = _gwx("FR", default_city="Paris")
    assert _reply(cmd, "Paris, FR", PARIS, default_location=True) == "Paris, FR: Clear 20°C"


def test_a_named_default_city_comes_out_of_the_budget():
    cmd = _gwx("FR", default_city="Paris")
    _reply(cmd, "Paris, FR", PARIS, default_location=True)
    assert cmd._get_open_meteo_weather_with_conditions.call_args.kwargs["location_prefix_len"] == len("Paris, FR: ")


def test_a_default_city_without_address_details_is_still_named():
    # The typed query itself labels the reply when the geocode kept no address.
    cmd = _gwx("FR", default_city="Paris")
    assert _reply(cmd, "Paris, FR", None, default_location=True) == "Paris, FR: Clear 20°C"


def test_a_typed_default_region_query_stays_unnamed():
    # The sender named the place themselves; only the fallback has to say it.
    cmd = _gwx("FR", default_city="Paris")
    assert _reply(cmd, "Paris, FR", PARIS) == "Clear 20°C"


@pytest.mark.parametrize(("content", "fallback", "location", "named"), [
    ("gwx", ("Paris, FR", False), "Paris, FR", True),
    ("gwx 5d", ("Paris, FR", False), "Paris, FR", True),  # an option alone still falls back
    ("gwx", ("48.85640,2.35140", True), "48.85640,2.35140", False),  # the sender's position
    ("gwx Lyon", None, "Lyon", False),
])
def test_only_the_default_city_fallback_asks_for_the_label(content, fallback, location, named):
    cmd = _gwx("FR", default_city="Paris")
    cmd._no_location_fallback = AsyncMock(return_value=fallback)
    cmd.get_weather_for_location = AsyncMock(return_value="ok")
    cmd._send_weather_reply = AsyncMock(return_value=True)
    cmd.record_execution = Mock()
    message = SimpleNamespace(content=content, sender_id="u1", sender_pubkey="pk", channel="general", is_dm=False)
    asyncio.run(cmd.execute(message))
    call = cmd.get_weather_for_location.await_args
    assert call.args[0] == location
    assert call.kwargs["default_location"] is named
    if fallback is None:
        cmd._no_location_fallback.assert_not_awaited()
