"""Tests for BaseCommand language auto-detection and the response-language
context manager (modules.commands.base_command)."""

import asyncio

import pytest

from modules.commands.base_command import BaseCommand
from tests.conftest import mock_message


class _FakeTranslator:
    """Minimal translator that tags keys with its language for assertions."""

    def __init__(self, language):
        self.language = language
        self.base_language = language.split("-")[0]

    def translate(self, key, **kwargs):
        return f"[{self.language}] {key}"

    def get_value(self, key):
        return None


class _ProbeCommand(BaseCommand):
    """Concrete command exposing translate() for the tests."""

    name = "probe"
    keywords = ["probe"]

    async def execute(self, message):  # pragma: no cover - not exercised here
        return True


def _make_command(command_mock_bot, *, enabled, current="en"):
    """Wire the mock bot with real translator-caching hooks and build a command."""
    if not command_mock_bot.config.has_section("Localization"):
        command_mock_bot.config.add_section("Localization")
    command_mock_bot.config.set(
        "Localization", "auto_detect_language", "true" if enabled else "false"
    )
    command_mock_bot.translator = _FakeTranslator(current)
    command_mock_bot.available_languages = lambda: {"en", "de", "fr", "it"}
    command_mock_bot.get_translator = lambda lang: _FakeTranslator(lang)
    return _ProbeCommand(command_mock_bot)


class TestDetectResponseLanguage:
    def test_disabled_returns_none(self, command_mock_bot):
        cmd = _make_command(command_mock_bot, enabled=False)
        msg = mock_message(content="ciao")
        assert cmd.detect_response_language(msg) is None

    def test_enabled_detects_different_language(self, command_mock_bot):
        cmd = _make_command(command_mock_bot, enabled=True)
        msg = mock_message(content="ciao")
        assert cmd.detect_response_language(msg) == "it"

    def test_same_as_default_returns_none(self, command_mock_bot):
        cmd = _make_command(command_mock_bot, enabled=True)
        msg = mock_message(content="hello there")  # english == default
        assert cmd.detect_response_language(msg) is None

    def test_unsupported_language_returns_none(self, command_mock_bot):
        # Spanish is detectable by keyword but has no file in available_languages.
        cmd = _make_command(command_mock_bot, enabled=True)
        msg = mock_message(content="hola")
        assert cmd.detect_response_language(msg) is None

    def test_mentions_are_stripped_before_detection(self, command_mock_bot):
        cmd = _make_command(command_mock_bot, enabled=True)
        msg = mock_message(content="@[TestBot] hallo")
        assert cmd.detect_response_language(msg) == "de"

    def test_empty_content_returns_none(self, command_mock_bot):
        cmd = _make_command(command_mock_bot, enabled=True)
        msg = mock_message(content="")
        assert cmd.detect_response_language(msg) is None


class TestRespondInSenderLanguage:
    def test_binds_translator_inside_and_restores_after(self, command_mock_bot):
        cmd = _make_command(command_mock_bot, enabled=True, current="en")
        msg = mock_message(content="ciao")

        # Outside the block: default (English) translator.
        assert cmd.translate("k") == "[en] k"

        with cmd.respond_in_sender_language(msg):
            assert cmd.translate("k") == "[it] k"

        # Restored afterwards.
        assert cmd.translate("k") == "[en] k"

    def test_no_switch_keeps_default(self, command_mock_bot):
        cmd = _make_command(command_mock_bot, enabled=True, current="en")
        msg = mock_message(content="hello")  # english, no switch

        with cmd.respond_in_sender_language(msg):
            assert cmd.translate("k") == "[en] k"

    def test_disabled_keeps_default_inside_block(self, command_mock_bot):
        cmd = _make_command(command_mock_bot, enabled=False, current="en")
        msg = mock_message(content="ciao")

        with cmd.respond_in_sender_language(msg):
            assert cmd.translate("k") == "[en] k"

    @pytest.mark.asyncio
    async def test_concurrent_overrides_are_task_local(self, command_mock_bot):
        cmd = _make_command(command_mock_bot, enabled=True, current="en")

        async def render(content):
            with cmd.respond_in_sender_language(mock_message(content=content)):
                await asyncio.sleep(0)
                return cmd.translate("k")

        italian, german = await asyncio.gather(render("ciao"), render("hallo"))

        assert italian == "[it] k"
        assert german == "[de] k"
        assert cmd.translate("k") == "[en] k"
