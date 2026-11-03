"""Tests for modules.commands.vigilance_command."""

from unittest.mock import patch

import pytest

from modules.commands.vigilance_command import VigilanceCommand
from tests.conftest import mock_message


def _vigilance_section(bot, department="75"):
    """Add the config section the command reads its default department from."""
    if not bot.config.has_section("Vigilance_Service"):
        bot.config.add_section("Vigilance_Service")
    bot.config.set("Vigilance_Service", "department", department)
    bot.config.set("Vigilance_Service", "channel", "#general")


def _command(bot):
    bot.config.set("Bot", "command_prefix", "")
    return VigilanceCommand(bot)


class TestDepartmentResolution:
    """The command takes the department from the args, then from [Vigilance_Service]."""

    def test_department_from_args(self, command_mock_bot):
        cmd = _command(command_mock_bot)
        assert cmd._parse_department("25") == "25"

    def test_short_department_is_padded(self, command_mock_bot):
        cmd = _command(command_mock_bot)
        assert cmd._parse_department("1") == "01"

    def test_three_digit_corse_departments(self, command_mock_bot):
        cmd = _command(command_mock_bot)
        assert cmd._parse_department("201") == "201"

    def test_falls_back_to_configured_department(self, command_mock_bot):
        _vigilance_section(command_mock_bot, department="69")
        cmd = _command(command_mock_bot)
        assert cmd._parse_department("") == "69"

    def test_invalid_argument_falls_back_to_config(self, command_mock_bot):
        _vigilance_section(command_mock_bot, department="69")
        cmd = _command(command_mock_bot)
        assert cmd._parse_department("paris") == "69"

    def test_default_without_config_section(self, command_mock_bot):
        cmd = _command(command_mock_bot)
        assert cmd._parse_department("") == "75"

    def test_args_after_prefix_are_split_from_trigger(self, command_mock_bot):
        cmd = _command(command_mock_bot)
        trigger, args = cmd.split_trigger_and_args("vigi 25")
        assert trigger == "vigi"
        assert args == "25"


class TestKeywords:
    """'alert' belongs to alert_command; two plugins cannot claim the same keyword."""

    def test_keywords_do_not_include_alert(self, command_mock_bot):
        cmd = _command(command_mock_bot)
        assert "alert" not in cmd.keywords
        assert "alerte" not in cmd.keywords

    def test_keywords_are_vigi_and_vigilance(self, command_mock_bot):
        cmd = _command(command_mock_bot)
        assert set(cmd.keywords) == {"vigi", "vigilance"}

    def test_no_own_settings_schema(self, command_mock_bot):
        """The department is owned by [Vigilance_Service], not by a second key."""
        cmd = _command(command_mock_bot)
        assert cmd.settings_schema == []


class TestResponseFormatting:
    def test_emoji_and_department(self, command_mock_bot):
        cmd = _command(command_mock_bot)
        text = cmd._format_response("25", {"color": "Orange", "summary": "", "url": "x"})
        assert text.startswith("🟠 Vigilance Dept 25: Orange")

    def test_green_level(self, command_mock_bot):
        cmd = _command(command_mock_bot)
        text = cmd._format_response("75", {"color": "Vert", "summary": "", "url": ""})
        assert "✅" in text

    def test_unknown_color_uses_default_emoji(self, command_mock_bot):
        cmd = _command(command_mock_bot)
        text = cmd._format_response("75", {"color": "violet", "summary": "", "url": ""})
        assert "🌦️" in text

    def test_first_phenomenon_line_is_kept(self, command_mock_bot):
        cmd = _command(command_mock_bot)
        summary = "Vigilance orange : orages\n- Orages\n- Pluie-inondation"
        text = cmd._format_response("25", {"color": "Orange", "summary": summary, "url": ""})
        assert "Orages" in text
        assert "Pluie-inondation" not in text

    def test_response_is_capped_for_mesh(self, command_mock_bot):
        cmd = _command(command_mock_bot)
        summary = "Vigilance rouge:\n" + "- " + ("x" * 300)
        text = cmd._format_response("75", {"color": "Rouge", "summary": summary, "url": ""})
        assert len(text) <= 180
        assert text.endswith("...")


class TestExecute:
    @pytest.mark.asyncio
    async def test_execute_uses_argument_department(self, command_mock_bot):
        cmd = _command(command_mock_bot)
        msg = mock_message(content="vigi 25", is_dm=True)

        with patch(
            "modules.commands.vigilance_command.fetch_vigilance",
            return_value={
                "color": "Orange",
                "summary": "Vigilance orange : orages",
                "bulletin_date": None,
                "url": "https://vigilance.meteofrance.fr/fr/besancon",
            },
        ) as fetch:
            assert await cmd.execute(msg) is True

        assert fetch.call_args[0][0] == "25"
        sent = command_mock_bot.command_manager.send_response.call_args[0][1]
        assert "Dept 25" in sent
        assert "Orange" in sent

    @pytest.mark.asyncio
    async def test_execute_falls_back_to_configured_department(self, command_mock_bot):
        _vigilance_section(command_mock_bot, department="69")
        cmd = _command(command_mock_bot)
        msg = mock_message(content="vigi", is_dm=True)

        with patch(
            "modules.commands.vigilance_command.fetch_vigilance",
            return_value={"color": "Vert", "summary": "", "url": ""},
        ) as fetch:
            assert await cmd.execute(msg) is True

        assert fetch.call_args[0][0] == "69"

    @pytest.mark.asyncio
    async def test_execute_reports_fetch_failure_without_crashing(self, command_mock_bot):
        cmd = _command(command_mock_bot)
        msg = mock_message(content="vigi", is_dm=True)

        with patch(
            "modules.commands.vigilance_command.fetch_vigilance",
            side_effect=ConnectionResetError("connection reset"),
        ):
            assert await cmd.execute(msg) is False

        sent = command_mock_bot.command_manager.send_response.call_args[0][1]
        assert "Erreur vigilance" in sent

    def test_help_text_documents_the_usage(self, command_mock_bot):
        cmd = _command(command_mock_bot)
        assert "vigi 75" in cmd.get_help_text()
