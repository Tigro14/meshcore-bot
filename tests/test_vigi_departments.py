"""Default vigilance lookup covers every configured department without live radio sends."""

import sqlite3
from unittest.mock import AsyncMock, patch

import pytest

from modules.commands.vigilance_command import VigilanceCommand
from tests.conftest import mock_message
from tests.test_vigilance_changes import Metadata
from tests.test_vigilance_offline_groups import NOW, warning

CODES = ["29", "22", "35", "56"]


@pytest.fixture
def command(command_mock_bot, tmp_path):
    bot = command_mock_bot
    if not bot.config.has_section("Vigilance_Service"):
        bot.config.add_section("Vigilance_Service")
    for key, value in {
        "department": ",".join(CODES),
        "offline_cache": "true",
        "request_delay": "2",
        "api_key": "test-key",
    }.items():
        bot.config.set("Vigilance_Service", key, value)
    bot.db_manager = Metadata(tmp_path / "vigi.db")
    cmd = VigilanceCommand(bot)
    cmd.send_response = AsyncMock(return_value=True)
    return cmd


async def run(command, data, text="vigi"):
    with patch("modules.commands.vigilance_command.fetch_vigilance_multi", return_value=data) as fetch:
        assert await command.execute(mock_message(text, is_dm=True))
    return command.send_response.await_args.args[1], fetch


def test_default_uses_all_departments_and_deduplicates_configuration(command):
    assert command._requested_departments("") == CODES
    command.bot.config.set("Vigilance_Service", "department", "29, 22,29, 35,56")
    assert command._requested_departments("") == CODES
    assert command._requested_departments("2a") == ["2A"]


@pytest.mark.parametrize("trigger", ["vigi", "vigilance"])
async def test_all_green_is_one_compact_answer_after_checking_all_four(command, trigger):
    reply, fetch = await run(command, {code: warning("Vert") for code in CODES}, trigger)
    assert fetch.call_args.args == (CODES, "test-key")
    assert fetch.call_args.kwargs == {"delay": 2.0}
    assert reply == "✅ Vigilance 22/29/35/56 : Vert"
    assert len(reply.encode()) < 160
    assert all(command.bulletin_cache.get(code) for code in CODES)
    assert command.send_response.await_count == 1


async def test_explicit_department_still_only_checks_that_department(command):
    with (
        patch("modules.commands.vigilance_command.fetch_vigilance", return_value=warning("Vert")) as fetch,
        patch("modules.commands.vigilance_command.fetch_vigilance_multi") as multi,
    ):
        assert await command.execute(mock_message("vigi 35", is_dm=True))
    assert fetch.call_args.args == ("35", "test-key")
    multi.assert_not_called()
    assert "Dept 35" in command.send_response.await_args.args[1]
    assert command.bulletin_cache.get("29") is None


async def test_colors_and_risks_remain_distinct_and_severe_levels_are_first(command):
    reply, _ = await run(
        command,
        {"29": warning("Orange"), "22": warning("Rouge", risk="3"), "35": warning("Vert"), "56": warning("Vert")},
    )
    assert reply.startswith("🔴 Vigilance 22 : Rouge\nOrages : rouge")
    assert "🟠 Vigilance 29 : Orange\nVent : orange" in reply
    assert "✅ Vigilance 35/56 : Vert" in reply


async def test_all_active_phenomena_are_reported_and_different_levels_are_not_grouped(command):
    first = warning()
    first["phenomena"].append({"id": "3", "color": "Jaune"})
    second = warning()
    second["phenomena"].append({"id": "3", "color": "Orange"})
    reply, _ = await run(command, {"29": first, "22": second, "35": warning("Vert"), "56": warning("Vert")})
    assert "Vigilance 22/29" not in reply
    assert "Vent : orange" in reply and "Orages : jaune" in reply and "Orages : orange" in reply


async def test_outage_groups_identical_saved_bulletins_with_dates(command):
    for code in CODES:
        command.bulletin_cache.put(code, warning(), NOW)
    with patch("modules.commands.vigilance_command.fetch_vigilance_multi", side_effect=OSError("offline")):
        assert await command.execute(mock_message("vigi", is_dm=True))
    reply = command.send_response.await_args.args[1]
    assert reply.startswith("Bulletin non actualisé - Depts 22/29/35/56")
    assert reply.count("Dernier niveau connu") == 1
    assert "09/11/2026" in reply and "Vent : orange" in reply and "Situation actuelle inconnue" in reply


async def test_missing_department_is_historical_while_other_results_are_current(command):
    command.bulletin_cache.put("22", warning(), NOW)
    reply, _ = await run(command, {code: warning("Vert") for code in ["29", "35", "56"]})
    assert "✅ Vigilance 29/35/56 : Vert" in reply
    assert "Bulletin non actualisé - Dept 22" in reply
    assert "Dernier niveau connu : orange" in reply
    assert "22/29/35/56 : Vert" not in reply


async def test_expired_download_cannot_be_displayed_as_current_or_overwrite_cache(command):
    command.bulletin_cache.put("22", warning(), NOW)
    infos = {code: warning("Vert") for code in CODES}
    infos["22"] = warning("Vert", until=NOW - 1)
    reply, _ = await run(command, infos)
    assert "✅ Vigilance 29/35/56 : Vert" in reply
    assert "Bulletin non actualisé - Dept 22" in reply
    assert command.bulletin_cache.get("22")["info"]["color"] == "Orange"


async def test_saved_departments_with_different_dates_are_not_merged(command):
    for code in CODES:
        command.bulletin_cache.put(code, warning(issued=NOW - (7200 if code == "22" else 3600)), NOW)
    with patch("modules.commands.vigilance_command.fetch_vigilance_multi", side_effect=OSError("offline")):
        assert await command.execute(mock_message("vigi", is_dm=True))
    reply = command.send_response.await_args.args[1]
    assert "Depts 29/35/56" in reply and "Dept 22" in reply
    assert reply.count("Dernier niveau connu") == 2


async def test_no_cached_data_reports_all_four_as_unknown_without_inventing_green(command):
    with patch("modules.commands.vigilance_command.fetch_vigilance_multi", side_effect=OSError("offline")):
        assert await command.execute(mock_message("vigi", is_dm=True))
    reply = command.send_response.await_args.args[1]
    assert "Depts 22/29/35/56" in reply and "Aucun bulletin conservé" in reply
    assert "inconnue" in reply and "vert" not in reply.lower()


async def test_a_query_does_not_change_configuration_or_automated_alert_state(command):
    before = {section: dict(command.bot.config.items(section)) for section in command.bot.config.sections()}
    await run(command, {code: warning() for code in CODES})
    assert before == {section: dict(command.bot.config.items(section)) for section in command.bot.config.sections()}
    with sqlite3.connect(command.bot.db_manager.path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM bot_metadata WHERE key LIKE 'vigilance.alert.%'").fetchone()[0]
            == 0
        )
    command.bot.command_manager.send_channel_message.assert_not_called()
