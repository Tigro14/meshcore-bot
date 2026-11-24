"""Weather prose must keep the place returned by the weather capability."""

from unittest.mock import AsyncMock

import pytest

from modules.assistant.dispatcher import AssistantDispatcher
from tests.conftest import mock_message
from tests.test_assistant_integration import setup_bot


async def weather_reply(bot, place, source, rendered, *, default=False):
    commands, _ = setup_bot(bot)
    bot.config.set("Weather", "default_city", "Bretagne")
    bot.config.set("Weather", "default_location_label", "En Bretagne")
    captured = []

    async def execute(message):
        captured.append(message.content)
        await commands["wx"].send_response(message, source)
        return True

    commands["wx"].execute = AsyncMock(side_effect=execute)
    commands["llm"].service.rephrase_tool_result = AsyncMock(return_value=rendered)
    question = "météo" if default else f"météo {place} ?"
    dispatcher = commands["ask"].dispatcher
    result = await dispatcher._weather_tool(
        question, mock_message(content=question), {"location": "" if default else place, "period": "today"}
    )
    return result, captured, commands["llm"].service.rephrase_tool_result.await_args


@pytest.mark.asyncio
@pytest.mark.parametrize("place", ["Combrit", "Quimper", "Saint-Brieuc", "L'Haÿ-les-Roses"])
async def test_rejects_brest_replacing_the_provider_city(command_mock_bot, place):
    answer, called, prompt = await weather_reply(
        command_mock_bot,
        place,
        f"{place}, FR: Aujourd'hui: Couvert 13°C H:13°C L:6°C N19G31 69%RH",
        "À Brest aujourd'hui : ciel couvert, 13°C, de 6 à 13°C.",
    )
    assert called == ["wx " + place]
    assert place in answer
    assert "Brest" not in answer
    assert "Brest" not in prompt.kwargs["context"]


@pytest.mark.asyncio
async def test_accepts_correct_city_in_a_natural_sentence(command_mock_bot):
    prose = "Aujourd'hui à Combrit, ciel couvert, 13°C, de 6 à 13°C."
    answer, _, _ = await weather_reply(
        command_mock_bot, "Combrit", "Combrit, FR: Aujourd'hui: Couvert 13°C H:13°C L:6°C", prose
    )
    assert answer == prose


@pytest.mark.asyncio
async def test_does_not_accept_city_only_in_the_body_after_a_wrong_heading(command_mock_bot):
    answer, _, _ = await weather_reply(
        command_mock_bot,
        "Combrit",
        "Combrit, FR: Aujourd'hui: Couvert 13°C",
        "À Brest aujourd'hui : ciel couvert comme à Combrit, 13°C.",
    )
    assert "Combrit" in answer and "Brest" not in answer


@pytest.mark.asyncio
async def test_missing_location_in_prose_falls_back_to_labelled_source(command_mock_bot):
    answer, _, _ = await weather_reply(
        command_mock_bot, "Combrit", "Combrit, FR: Aujourd'hui: Couvert 13°C", "Ciel couvert, 13°C."
    )
    assert answer.startswith("Combrit")


@pytest.mark.asyncio
async def test_region_default_is_not_replaced_with_a_city(command_mock_bot):
    answer, called, _ = await weather_reply(
        command_mock_bot,
        "",
        "Merléac, FR: Aujourd'hui: Couvert 13°C",
        "À Brest aujourd'hui : ciel couvert, 13°C.",
        default=True,
    )
    assert called == ["wx Bretagne"]
    assert answer.startswith("En Bretagne")
    assert "Brest" not in answer and "Merléac" not in answer


@pytest.mark.asyncio
async def test_correct_region_default_remains_natural(command_mock_bot):
    answer, _, _ = await weather_reply(
        command_mock_bot,
        "",
        "Merléac, FR: Aujourd'hui: Couvert 13°C",
        "En Bretagne aujourd'hui : ciel couvert, 13°C.",
        default=True,
    )
    assert answer == "En Bretagne aujourd'hui : ciel couvert, 13°C."


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("Combrit, FR: Aujourd'hui: Couvert 13°C", "Combrit"),
        ("Brest demain : 16°C", "Brest"),
        ("Saint-Brieuc, FR: Demain: Couvert", "Saint-Brieuc"),
        ("En Bretagne: Aujourd'hui: Couvert", "En Bretagne"),
        ("Aujourd'hui: Couvert 13°C", ""),
        ("Demain: Couvert 13°C", ""),
        ("Aucune prévision disponible.", ""),
    ],
)
def test_source_location_is_metadata_not_an_example(source, expected):
    assert AssistantDispatcher._weather_source_location(source) == expected
