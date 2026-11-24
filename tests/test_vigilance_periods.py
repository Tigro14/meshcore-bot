"""Dated J0/J1 bulletins, independent failures, clock changes and captured radio delivery."""

from copy import deepcopy
from datetime import date, datetime, time, timedelta
from unittest.mock import AsyncMock, Mock, patch

import pytest
import requests

from modules.commands.vigilance_command import VigilanceCommand
from modules.service_plugins.vigilance_service import (
    VigilanceService,
    _product_entry,
    fetch_vigilance_multi,
    fetch_vigilance_site_api,
)
from modules.vigilance_alert_policy import fingerprint
from modules.vigilance_periods import PARIS, attach_period, calendar_days, period_key, risk_lines
from tests.conftest import mock_message
from tests.test_vigilance_changes import Metadata, check
from tests.test_vigilance_offline_groups import NOW, warning
from tests.test_vigilance_service import _make_bot

CODES = ["29", "22", "35", "56"]
TODAY, TOMORROW = calendar_days(NOW)


def epoch(day, hour=0):
    return datetime.combine(date.fromisoformat(day), time(hour), PARIS).timestamp()


def raw(day, code="29", color=3, risk="1"):
    finish = epoch((date.fromisoformat(day) + timedelta(days=1)).isoformat())
    return {
        "domain_id": code,
        "update_time": NOW - 3600,
        "end_validity_time": int(finish),
        "color_max": color,
        "phenomenons_items": [{"phenomenon_id": risk, "phenomenon_max_color_id": color}],
        "timelaps": [
            {
                "phenomenon_id": risk,
                "timelaps_items": [
                    {"begin_time": int(epoch(day, 8)), "end_time": int(epoch(day, 18)), "color_id": color}
                ],
            }
        ],
    }


def period(day, color="Vert"):
    data = raw(day, color={"Vert": 1, "Jaune": 2, "Orange": 3, "Rouge": 4}[color])
    info = warning(color, until=data["end_validity_time"])
    return attach_period(info, day, data["timelaps"])


def batch(current="Vert", future="Vert", today=TODAY, tomorrow=TOMORROW):
    return {code: {"periods": [period(today, current), period(tomorrow, future)]} for code in CODES}


@pytest.fixture
def monitor(tmp_path):
    db = Metadata(tmp_path / "monitor.db")

    def make():
        bot = _make_bot(
            department=",".join(CODES),
            channel="#bretagne",
            poll_interval="3600000",
            alert_levels="Jaune,Orange,Rouge",
            notify_on_change="true",
            offline_cache="true",
            group_similar_alerts="true",
            include_tomorrow="true",
        )
        bot.db_manager = db
        service = VigilanceService(bot)
        service.send_external_notifications = AsyncMock()
        return service

    return make


@pytest.fixture
def command(command_mock_bot, tmp_path):
    bot = command_mock_bot
    if not bot.config.has_section("Vigilance_Service"):
        bot.config.add_section("Vigilance_Service")
    for key, value in {"department": ",".join(CODES), "offline_cache": "true", "include_tomorrow": "true"}.items():
        bot.config.set("Vigilance_Service", key, value)
    bot.db_manager = Metadata(tmp_path / "command.db")
    cmd = VigilanceCommand(bot)
    cmd.send_response = AsyncMock(return_value=True)
    return cmd


@pytest.mark.parametrize("echeance,day", [("J0", TODAY), ("J1", TOMORROW)])
def test_site_selects_the_requested_day_and_preserves_official_windows(echeance, day):
    response = Mock()
    response.json.return_value = raw(day)
    with (
        patch("modules.service_plugins.vigilance_service.requests.get", return_value=response) as get,
        patch("modules.service_plugins.vigilance_service.time.time", return_value=NOW),
    ):
        result = fetch_vigilance_site_api("29", "session", "https://example.test", echeance=echeance)
    assert get.call_args.kwargs["params"]["echeance"] == echeance
    assert result["period_date"] == day
    assert result["windows"][0]["begin"] == epoch(day, 8)
    assert risk_lines(result, {"1": "Vent"}) == ["Vent : orange 08:00-18:00"]


def product():
    periods = []
    for echeance, day in [("J1", TOMORROW), ("J", TODAY)]:
        domains = []
        for code in CODES:
            data = raw(day, code=code, color=3 if echeance == "J1" else 1)
            domains.append(
                {
                    "domain_id": code,
                    "max_color_id": data["color_max"],
                    "phenomenon_items": [
                        {
                            "phenomenon_id": "1",
                            "phenomenon_max_color_id": data["color_max"],
                            "timelaps_items": data["timelaps"][0]["timelaps_items"],
                        }
                    ],
                }
            )
        periods.append(
            {
                "echeance": echeance,
                "end_validity_time": datetime.fromtimestamp(data["end_validity_time"], PARIS).isoformat(),
                "timelaps": {"domain_ids": domains},
            }
        )
    return {"update_time": datetime.fromtimestamp(NOW - 3600, PARIS).isoformat(), "periods": periods}


def test_official_product_selects_by_echeance_even_when_tomorrow_is_first():
    with patch("modules.service_plugins.vigilance_service.time.time", return_value=NOW):
        assert _product_entry("29", product(), echeance="J0")["color"] == "Vert"
        assert _product_entry("29", product(), echeance="J1")["color"] == "Orange"
        incomplete = product()
        incomplete["periods"] = incomplete["periods"][1:]
        with pytest.raises(ValueError, match="no block"):
            _product_entry("29", incomplete, echeance="J1")


def test_one_bad_department_or_day_does_not_drop_the_other_six_periods():
    page = Mock()
    page.cookies = {"mfsession": "test"}

    def fetch(code, *args, echeance=None, **kwargs):
        if (code, echeance) in {("29", "J1"), ("22", "J0")}:
            raise requests.Timeout("test")
        return period(TODAY if echeance == "J0" else TOMORROW)

    with (
        patch("modules.service_plugins.vigilance_service.requests.get", return_value=page),
        patch("modules.service_plugins.vigilance_service.fetch_vigilance_site_api", side_effect=fetch) as get,
    ):
        result = fetch_vigilance_multi(CODES, include_tomorrow=True)
    assert get.call_count == 8
    assert len(result) == 4 and sum(len(data["periods"]) for data in result.values()) == 6


def test_bad_official_department_is_isolated_with_one_national_download():
    data = product()
    data["periods"][1]["timelaps"]["domain_ids"][1].pop("max_color_id")
    with (
        patch("modules.service_plugins.vigilance_service._fetch_product", return_value=data) as fetch,
        patch("modules.service_plugins.vigilance_service.time.time", return_value=NOW),
    ):
        result = fetch_vigilance_multi(CODES, "key", include_tomorrow=True)
    fetch.assert_called_once()
    assert sum(len(info["periods"]) for info in result.values()) == 7


def test_legacy_collection_also_keeps_other_departments_after_an_error():
    page = Mock()
    page.cookies = {"mfsession": "test"}

    def fetch(code, *args, **kwargs):
        if code == "22":
            raise requests.Timeout("test")
        return warning("Vert")

    with (
        patch("modules.service_plugins.vigilance_service.requests.get", return_value=page),
        patch("modules.service_plugins.vigilance_service.fetch_vigilance_site_api", side_effect=fetch),
    ):
        result = fetch_vigilance_multi(CODES)
    assert set(result) == {"29", "35", "56"}


def test_session_page_failure_uses_another_department_page():
    failed = Mock()
    failed.raise_for_status.side_effect = requests.HTTPError("unavailable")
    page = Mock()
    page.cookies = {"mfsession": "test"}
    with (
        patch("modules.service_plugins.vigilance_service.requests.get", side_effect=[failed, page]) as pages,
        patch("modules.service_plugins.vigilance_service.fetch_vigilance_site_api", return_value=warning("Vert")),
    ):
        assert len(fetch_vigilance_multi(CODES)) == 4
    assert pages.call_count == 2


def test_a_wrong_day_is_rejected_instead_of_relabelling_an_old_bulletin():
    info = warning(until=epoch(TOMORROW))
    with pytest.raises(ValueError, match="journée"):
        attach_period(info, TOMORROW, [])


@pytest.mark.parametrize("day,hours", [("2026-03-29", 23), ("2026-10-25", 25)])
def test_day_boundaries_follow_paris_daylight_saving(day, hours):
    end = epoch((date.fromisoformat(day) + timedelta(days=1)).isoformat())
    result = attach_period(warning(until=end), day, [])
    assert result["valid_until"] - result["period_begin"] == hours * 3600


def test_risk_without_a_timeline_is_reported_without_inventing_hours():
    data = raw(TOMORROW, risk="4")
    data["timelaps"][0]["timelaps_items"] = []
    response = Mock()
    response.json.return_value = data
    with (
        patch("modules.service_plugins.vigilance_service.requests.get", return_value=response),
        patch("modules.service_plugins.vigilance_service.time.time", return_value=NOW),
    ):
        result = fetch_vigilance_site_api("29", "session", "https://example.test", echeance="J1")
    assert result["phenomena"] == [{"id": "4", "color": "Orange"}]
    assert risk_lines(result, {"4": "Crues"}) == ["Crues : orange"]


def test_group_fingerprint_includes_date_and_windows_but_not_publication_time():
    first = period(TODAY, "Orange")
    other = deepcopy(first)
    other["issued_at"] += 600
    assert fingerprint(first) == fingerprint(other)
    assert fingerprint(first) != fingerprint(period(TOMORROW, "Orange"))
    other["windows"][0]["end"] -= 3600
    assert fingerprint(first) != fingerprint(other)


async def test_tomorrow_is_announced_even_when_today_is_green_and_restart_does_not_repeat(monitor):
    service = monitor()
    messages = await check(service, batch(future="Orange"), NOW)
    assert len(messages) == 1 and "Demain" in messages[0] and "8-18h" in messages[0]
    assert "22/29/35/56" in messages[0]
    assert service.alert_policy.state(period_key("29", TOMORROW))["color"] == "Orange"
    assert await check(monitor(), batch(future="Orange"), NOW + 3600) == []


async def test_today_and_tomorrow_are_not_merged_into_a_timeless_alert(monitor):
    messages = await check(monitor(), batch("Orange", "Orange"), NOW)
    assert len(messages) == 2
    assert sum(date.fromisoformat(TODAY).strftime('%d/%m') in message for message in messages) == 1
    assert sum("Demain" in message for message in messages) == 1


async def test_green_today_does_not_close_tomorrows_alert(monitor):
    service = monitor()
    await check(service, batch("Orange", "Orange"), NOW)
    service = monitor()
    messages = await check(service, batch("Vert", "Orange"), NOW + 3600)
    assert len(messages) == 1 and "Fin de vigilance" in messages[0] and date.fromisoformat(TODAY).strftime('%d/%m') in messages[0]
    assert service.alert_policy.state(period_key("29", TOMORROW))["color"] == "Orange"


async def test_missing_future_data_keeps_the_previous_future_risk(monitor):
    service = monitor()
    await check(service, batch(future="Orange"), NOW)
    service = monitor()
    partial = {code: {"periods": [period(TODAY)]} for code in CODES}
    messages = await check(service, partial, NOW + 3600)
    assert len(messages) == 1 and "non actualisable" in messages[0] and "Demain" in messages[0]
    assert "@" not in messages[0] and "retour au vert" not in messages[0]
    assert service.alert_policy.state(period_key("29", TOMORROW))["color"] == "Orange"


async def test_midnight_keeps_the_same_absolute_forecast_state(monitor):
    service = monitor()
    await check(service, batch(future="Orange"), NOW)
    next_day = (date.fromisoformat(TOMORROW) + timedelta(days=1)).isoformat()
    messages = await check(monitor(), batch("Orange", "Vert", TOMORROW, next_day), epoch(TOMORROW) + 60)
    assert messages == []


async def test_unpublished_next_day_before_six_is_not_an_outage_broadcast(monitor):
    partial = {code: {"periods": [period(TODAY)]} for code in CODES}
    for info in partial.values():
        info["periods"][0]["issued_at"] = epoch(TODAY) - 3600
    assert await check(monitor(), partial, epoch(TODAY) + 5 * 3600) == []


def test_dated_messages_do_not_expose_internal_keys_without_grouping(monitor):
    service = monitor()
    with patch("modules.service_plugins.vigilance_service.time.time", return_value=NOW):
        for text in [
            service._format_change_message(period_key("29", TOMORROW), period(TOMORROW, "Orange"), "new"),
            service._format_message(period_key("29", TOMORROW), period(TOMORROW, "Orange")),
        ]:
            assert "Demain" in text and "Dept 29" in text and "@" not in text


async def test_vigi_shows_both_days_and_all_departments_in_one_response(command):
    with (
        patch("modules.commands.vigilance_command.fetch_vigilance_multi", return_value=batch()) as fetch,
        patch("modules.commands.vigilance_command.time.time", return_value=NOW),
    ):
        assert await command.execute(mock_message("vigi", is_dm=True))
    assert fetch.call_args.kwargs["include_tomorrow"] is True
    reply = command.send_response.await_args.args[1]
    assert "Aujourd'hui" in reply and "Demain" in reply and reply.count("22/29/35/56") == 2
    assert len(reply.encode()) <= 158 and "@" not in reply


async def test_vigi_partial_failure_keeps_current_results_and_dated_history(command):
    with (
        patch("modules.commands.vigilance_command.fetch_vigilance_multi", return_value=batch(future="Orange")),
        patch("modules.commands.vigilance_command.time.time", return_value=NOW),
    ):
        assert await command.execute(mock_message("vigi", is_dm=True))
    partial = batch()
    partial["22"]["periods"] = partial["22"]["periods"][:1]
    with (
        patch("modules.commands.vigilance_command.fetch_vigilance_multi", return_value=partial),
        patch("modules.commands.vigilance_command.time.time", return_value=NOW + 3600),
    ):
        assert await command.execute(mock_message("vigi", is_dm=True))
    reply = command.send_response.await_args.args[1]
    assert "Aujourd'hui" in reply and "29/35/56 - Demain" in reply
    assert "Bulletin non actualisé - Dept 22 - Demain" in reply and "Dernier niveau connu : orange" in reply


async def test_offline_after_midnight_uses_saved_tomorrow_as_today_not_as_the_new_tomorrow(command):
    with (
        patch("modules.commands.vigilance_command.fetch_vigilance_multi", return_value=batch(future="Orange")),
        patch("modules.commands.vigilance_command.time.time", return_value=NOW),
    ):
        assert await command.execute(mock_message("vigi", is_dm=True))
    with (
        patch("modules.commands.vigilance_command.fetch_vigilance_multi", side_effect=OSError("offline")),
        patch("modules.commands.vigilance_command.time.time", return_value=epoch(TOMORROW) + 60),
    ):
        assert await command.execute(mock_message("vigi", is_dm=True))
    reply = command.send_response.await_args.args[1]
    assert "Bulletin non actualisé - Depts 22/29/35/56 - Aujourd'hui" in reply
    assert "Dernier niveau connu : orange" in reply
    assert "Demain" in reply and "Aucun bulletin conservé" in reply
