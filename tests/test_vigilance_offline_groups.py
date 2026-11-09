"""Vigilance cache, outage notices and shared broadcasts without live RF sends."""

import json
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch

import pytest

from modules.commands.vigilance_command import VigilanceCommand
from modules.service_plugins.vigilance_service import (
    VigilanceService,
    _product_entry,
    fetch_vigilance_site_api,
)
from modules.vigilance_cache import AvailabilityNotices, VigilanceCache, usable
from tests.conftest import mock_message
from tests.test_vigilance_changes import Metadata, check, info
from tests.test_vigilance_service import API_PRODUCT, SITE_API_DATA, _make_bot

NOW = 1794229200


def warning(color="Orange", *, issued=NOW - 3600, until=NOW + 86400, risk="1"):
    data = info(color)
    data.update(
        issued_at=issued,
        valid_until=until,
        bulletin_date=datetime.fromtimestamp(issued, timezone.utc),
        bulletin={
            "text": "Bulletin complet",
            "advices": ["Conseil officiel"],
            "consequences": ["Conséquence"],
            "timelaps": [{"risk": risk}],
        },
    )
    if color != "Vert":
        data["phenomena"] = [{"id": risk, "color": color}]
    return data


@pytest.fixture
def cache(tmp_path):
    return VigilanceCache(Metadata(tmp_path / "cache.db"), Mock())


@pytest.fixture
def monitor(tmp_path):
    db = Metadata(tmp_path / "monitor.db")

    def make(**overrides):
        settings = {
            "department": "29,22,35,56",
            "channel": "#bretagne",
            "poll_interval": "3600000",
            "alert_levels": "Jaune,Orange,Rouge",
            "notify_on_change": "true",
            "reminder_interval": "86400",
            "offline_cache": "true",
            "group_similar_alerts": "true",
        }
        settings.update(overrides)
        bot = _make_bot(**settings)
        bot.db_manager = db
        service = VigilanceService(bot)
        service.send_external_notifications = AsyncMock()
        return service

    return make


def test_full_bulletin_survives_restart_with_publication_and_validity(cache):
    data = warning()
    assert cache.put("29", data, NOW)
    restarted = VigilanceCache(cache.db, Mock())
    record = restarted.get("29")
    assert record["info"]["bulletin"] == data["bulletin"]
    assert record["info"]["issued_at"] == NOW - 3600
    assert record["info"]["valid_until"] == NOW + 86400
    assert record["fetched_at"] == NOW
    record["info"]["bulletin"]["text"] = "changed outside cache"
    assert restarted.get("29")["info"]["bulletin"]["text"] == "Bulletin complet"


def test_older_or_expired_download_cannot_replace_a_newer_bulletin(cache):
    assert cache.put("29", warning(), NOW)
    assert not cache.put("29", warning("Vert", issued=NOW - 7200), NOW + 60)
    assert not cache.put("29", warning("Vert", until=NOW - 1), NOW + 60)
    assert cache.get("29")["info"]["color"] == "Orange"


def test_historical_answers_are_never_presented_as_current(cache):
    cache.put("29", warning(), NOW)
    text = cache.historical_reply("29", NOW + 60, timezone.utc)
    assert "non actualisé" in text and "Dernier niveau connu : orange" in text
    assert "Situation actuelle inconnue" in text and "Validité annoncée" in text
    expired = cache.historical_reply("29", NOW + 86400, timezone.utc)
    assert "périmé" in expired and "Validité échue" in expired
    assert "Situation actuelle inconnue" in expired


def test_missing_cache_does_not_invent_green(cache):
    reply = cache.historical_reply("29", NOW, timezone.utc)
    assert "Aucun bulletin conservé" in reply and "inconnue" in reply
    assert "vert" not in reply.lower()


def test_unknown_validity_has_a_conservative_freshness_limit(cache):
    data = warning()
    data.pop("valid_until")
    cache.put("29", data, NOW)
    assert not usable(data, NOW + 86400)
    assert cache.status("29", NOW + 86400) == "old"
    assert "Bulletin ancien" in cache.historical_reply("29", NOW + 86400, timezone.utc)


@pytest.mark.parametrize("bad", ["not json", "{}", '{"info":{"color":"Green"}}'])
def test_corrupted_cache_is_not_a_current_bulletin(cache, bad):
    cache.db.set_metadata(cache._key("29"), bad)
    assert cache.get("29") is None


def test_site_source_keeps_the_complete_json_and_unambiguous_utc_time():
    raw = deepcopy(SITE_API_DATA)
    raw["advices"] = ["Conseil"]
    raw["end_validity_time"] = raw["update_time"] + 86400
    response = Mock()
    response.json.return_value = raw
    with patch("modules.service_plugins.vigilance_service.requests.get", return_value=response):
        data = fetch_vigilance_site_api("95", "not-stored", "https://example.test")
    assert data["bulletin"] == raw and data["issued_at"] == raw["update_time"]
    assert "not-stored" not in json.dumps(data["bulletin"])


def test_official_source_keeps_the_whole_product():
    product = deepcopy(API_PRODUCT["product"])
    assert _product_entry("95", product)["bulletin"] == product


async def test_identical_alerts_share_one_broadcast_and_each_state_is_saved(monitor):
    service = monitor()
    messages = await check(service, {code: warning() for code in ["29", "22", "35", "56"]}, NOW)
    assert len(messages) == 1 and "22/29/35/56" in messages[0]
    for code in ["29", "22", "35", "56"]:
        assert service.alert_policy.state(code)["color"] == "Orange"
    restarted = monitor()
    assert await check(restarted, {code: warning() for code in ["29", "22", "35", "56"]}, NOW + 3600) == []


async def test_different_levels_and_risks_stay_separate_and_red_is_first(monitor):
    service = monitor()
    messages = await check(
        service, {"29": warning("Jaune"), "22": warning("Rouge"), "35": warning("Jaune", risk="3")}, NOW
    )
    # Missing 56 gets its own availability notice, not a made-up weather level.
    weather = [m for m in messages if "Vigilance " in m and "non actualisable" not in m]
    assert len(weather) == 3 and "ROUGE" in weather[0]
    assert any("Orages" in m for m in weather)


async def test_only_departments_due_an_alert_are_grouped(monitor):
    service = monitor()
    await check(service, {"29": warning(), "22": warning("Vert"), "35": warning("Vert"), "56": warning("Vert")}, NOW)
    messages = await check(
        service, {"29": warning(), "22": warning(), "35": warning("Vert"), "56": warning("Vert")}, NOW + 3600
    )
    assert len(messages) == 2 and "Dept 22" in messages[-1]
    assert "29" not in messages[-1]
    assert service.alert_policy.state("29")["sent_at"] == NOW


async def test_failed_group_does_not_advance_any_department(monitor):
    service = monitor()
    service.bot.command_manager.send_channel_message.return_value = False
    await check(service, {code: warning() for code in ["29", "22", "35", "56"]}, NOW)
    assert all(service.alert_policy.state(code) is None for code in ["29", "22", "35", "56"])
    service = monitor()
    assert len(await check(service, {code: warning() for code in ["29", "22", "35", "56"]}, NOW + 3600)) == 1


async def test_grouped_clear_is_once_and_survives_restart(monitor):
    service = monitor()
    codes = ["29", "22", "35", "56"]
    await check(service, {code: warning() for code in codes}, NOW)
    service = monitor()
    messages = await check(service, {code: warning("Vert") for code in codes}, NOW + 3600)
    assert len(messages) == 1 and "Fin de vigilance" in messages[0] and "22/29/35/56" in messages[0]
    assert await check(monitor(), {code: warning("Vert") for code in codes}, NOW + 7200) == []


async def test_initial_green_stays_silent_but_is_cached(monitor):
    service = monitor()
    assert await check(service, {code: warning("Vert") for code in ["29", "22", "35", "56"]}, NOW) == []
    assert all(service.bulletin_cache.get(code) is not None for code in ["29", "22", "35", "56"])


async def test_outage_is_announced_once_across_restart_and_expiry_gets_one_update(monitor):
    service = monitor()
    codes = ["29", "22", "35", "56"]
    await check(service, {code: warning("Vert", until=NOW + 3600) for code in codes}, NOW)
    messages = await check(service, None, NOW + 60)
    assert len(messages) == 1 and "non actualisable" in messages[0]
    assert "Situation actuelle inconnue" in messages[0]
    service = monitor()
    assert await check(service, None, NOW + 120) == []
    messages = await check(service, None, NOW + 3600)
    assert len(messages) == 1 and "périmés" in messages[0]
    assert await check(monitor(), None, NOW + 7200) == []


async def test_failed_outage_notice_is_retried(monitor):
    service = monitor()
    service.bot.command_manager.send_channel_message.return_value = False
    await check(service, None, NOW)
    assert service.availability.last is None
    service = monitor()
    assert len(await check(service, None, NOW + 3600)) == 1


async def test_outage_without_cache_is_explicit_and_does_not_clear_risk(monitor):
    service = monitor()
    messages = await check(service, None, NOW)
    assert len(messages) == 1 and "Aucun bulletin conservé" in messages[0]
    assert "retour au vert" not in messages[0]


async def test_recovery_uses_fresh_data_and_announces_the_end_of_the_outage(monitor):
    codes = ["29", "22", "35", "56"]
    service = monitor()
    await check(service, {code: warning() for code in codes}, NOW)
    await check(service, None, NOW + 3600)
    service = monitor()
    data = {code: warning("Vert", issued=NOW + 3600) for code in codes}
    messages = await check(service, data, NOW + 7200)
    assert sum("Fin de vigilance" in m for m in messages) == 1
    assert messages[-1] == "Actualisation de la vigilance rétablie."
    assert await check(monitor(), data, NOW + 10800) == []


async def test_stale_successful_http_answer_is_not_a_recovery_or_a_green_clear(monitor):
    codes = ["29", "22", "35", "56"]
    service = monitor()
    await check(service, {code: warning() for code in codes}, NOW)
    service = monitor()
    messages = await check(service, {code: warning("Vert", issued=NOW - 7200) for code in codes}, NOW + 3600)
    assert len(messages) == 1 and "non actualisable" in messages[0]
    assert all(service.alert_policy.state(code)["color"] == "Orange" for code in codes)


async def test_partial_failure_keeps_old_data_and_still_updates_other_departments(monitor):
    service = monitor()
    codes = ["29", "22", "35", "56"]
    await check(service, {code: warning("Vert") for code in codes}, NOW)
    partial = {"29": warning("Rouge", issued=NOW), "35": warning("Vert"), "56": warning("Vert")}
    messages = await check(service, partial, NOW + 3600)
    assert len(messages) == 2 and "ROUGE" in messages[0]
    assert "Depts 22" in messages[1]
    assert service.bulletin_cache.get("22")["info"]["color"] == "Vert"


async def test_cached_data_never_triggers_a_daily_live_reminder(monitor):
    service = monitor()
    codes = ["29", "22", "35", "56"]
    await check(service, {code: warning(until=NOW + 2 * 86400) for code in codes}, NOW)
    messages = await check(service, None, NOW + 86400)
    assert len(messages) == 2 and "non actualisable" in messages[-1]
    assert "Rappel" not in messages[-1]
    assert service.alert_policy.state("29")["sent_at"] == NOW


@pytest.mark.parametrize("expired", [False, True])
async def test_vigi_answers_offline_from_the_shared_persistent_cache(command_mock_bot, tmp_path, expired):
    bot = command_mock_bot
    if not bot.config.has_section("Vigilance_Service"):
        bot.config.add_section("Vigilance_Service")
    bot.config.set("Vigilance_Service", "offline_cache", "true")
    bot.config.set("Vigilance_Service", "department", "29,22,35,56")
    bot.config.set("Bot", "timezone", "Europe/Paris")
    bot.db_manager = Metadata(tmp_path / "command.db")
    cache = VigilanceCache(bot.db_manager, Mock())
    cache.put("29", warning(until=NOW + 3600), NOW)
    command = VigilanceCommand(bot)
    command.send_response = AsyncMock(return_value=True)
    assert command.requires_internet is False
    clock = NOW + 7200 if expired else NOW + 60
    with (
        patch("modules.commands.vigilance_command.fetch_vigilance", side_effect=OSError("offline")),
        patch("modules.commands.vigilance_command.time.time", return_value=clock),
    ):
        assert await command.execute(mock_message("vigi 29", is_dm=True))
    reply = command.send_response.await_args.args[1]
    assert "Dept 29" in reply and "Dernier niveau connu : orange" in reply
    assert ("périmé" if expired else "non actualisé") in reply
    assert "Vent : orange" in reply
    assert "Situation actuelle inconnue" in reply


def test_availability_state_is_target_scoped(tmp_path):
    db = Metadata(tmp_path / "targets.db")
    first = AvailabilityNotices(db, Mock(), "first")
    first.record_sent({"29": "missing"})
    assert not AvailabilityNotices(db, Mock(), "first").due({"29": "missing"})
    assert AvailabilityNotices(db, Mock(), "other").due({"29": "missing"})


async def test_same_overall_color_with_different_phenomenon_levels_is_not_grouped(monitor):
    service = monitor()
    first = warning()
    second = warning()
    first["phenomena"].append({"id": "3", "color": "Jaune"})
    second["phenomena"].append({"id": "3", "color": "Orange"})
    messages = await check(service, {"29": first, "22": second, "35": warning("Vert"), "56": warning("Vert")}, NOW)
    assert len(messages) == 2 and all("Depts" not in message for message in messages)


async def test_mixed_new_and_reminder_group_is_not_labelled_as_only_a_reminder(monitor):
    service = monitor()
    codes = ["29", "22", "35", "56"]
    await check(
        service, {code: warning("Orange" if code == "29" else "Vert", until=NOW + 3 * 86400) for code in codes}, NOW
    )
    messages = await check(
        service,
        {
            code: warning("Orange" if code in ["29", "22"] else "Vert", issued=NOW + 86000, until=NOW + 3 * 86400)
            for code in codes
        },
        NOW + 86400,
    )
    assert len(messages) == 2 and "Depts 22/29" in messages[-1] and "Rappel" not in messages[-1]


async def test_recovery_with_unchanged_weather_only_announces_availability_once(monitor):
    service = monitor()
    data = {code: warning() for code in ["29", "22", "35", "56"]}
    await check(service, data, NOW)
    await check(service, None, NOW + 3600)
    service = monitor()
    messages = await check(service, data, NOW + 7200)
    assert messages == ["Actualisation de la vigilance rétablie."]
    assert await check(monitor(), data, NOW + 10800) == []
