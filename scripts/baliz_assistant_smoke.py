#!/usr/bin/env python3
"""Exercise real assistant commands without a radio, optionally against Ollama.

All mesh data and RF messages are fixtures. No production database is opened.
"""
import argparse
import asyncio
import configparser
import json
import logging
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.commands.ask_command import AskCommand
from modules.commands.llm_command import LlmCommand
from modules.commands.mesh_command import MeshCommand
from modules.commands.path_command import PathCommand
from modules.commands.test_command import TestCommand
from modules.commands.wx_command import WxCommand
from modules.models import MeshMessage


async def run(args):
    logging.basicConfig(level=logging.WARNING)
    with tempfile.TemporaryDirectory(prefix="baliz-assistant-") as directory:
        db = Path(directory) / "fixture.db"
        index = Path(directory) / "wiki.jsonl"
        index.write_text(json.dumps({
            "path": "configuration/test", "title": "Configuration de démonstration",
            "chunks": ["Sur le banc de test Baliz, demo_region vaut TEST_109. Ce paramètre est fictif."],
        }) + "\n")
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE complete_contact_tracking (name TEXT, public_key TEXT, role TEXT, latitude REAL, longitude REAL, last_heard TEXT, is_currently_tracked INTEGER)")
            conn.executemany("INSERT INTO complete_contact_tracking VALUES (?, ?, 'repeater', NULL, NULL, datetime('now'), 1)",
                             [("Alpha", "a" * 64), ("Bravo", "b" * 64)])
        @contextmanager
        def connection():
            conn = sqlite3.connect(db)
            try:
                yield conn
            finally:
                conn.close()
        config = configparser.ConfigParser()
        config.read_dict({
            "Bot": {"bot_name": "Baliz-Test", "command_prefix": ""},
            "Channels": {"monitor_channels": "test", "respond_to_dms": "true"},
            "Keywords": {}, "Path_Command": {},
            "Ask_Command": {"enabled": "true", "aliases": "baliz", "route_timeout_seconds": str(min(300, args.timeout * 2 + 15))},
            "Mesh_Command": {"enabled": "true"},
            "Llm_Command": {
                "enabled": str(args.live).lower(), "endpoint": args.endpoint, "model": args.model,
                "reasoning_effort": args.reasoning_effort,
                "timeout_seconds": str(args.timeout), "max_tokens": "200", "cpu_temp_threshold": "0",
                "context_window_seconds": "0", "wiki_rag_enabled": "true",
                "wiki_rag_index_path": str(args.wiki_index or index), "wiki_refresh_interval_seconds": "0",
                "context_include_weather": "false", "context_include_commands": "false",
                "system_prompt": "Réponds brièvement en français. Tu es Baliz sur un banc de test.",
            },
            "Weather": {
                "weather_provider": "openmeteo", "default_country": "FR",
                "temperature_unit": "celsius", "wind_speed_unit": "kmh",
                "precipitation_unit": "mm",
            },
            # Direct wx stays hidden; ASK still has access to its capability.
            "Wx_Command": {"enabled": "false"},
            # Direct test stays hidden; ASK still has access to reception data.
            "Test_Command": {"enabled": "false"},
        })
        bot = MagicMock()
        bot.config = config
        bot.logger = logging.getLogger("baliz-smoke")
        bot.translator.get_value = Mock(return_value=None)
        bot.translator.translate = Mock(side_effect=lambda k, **kw: k)
        bot.db_manager.connection = connection
        # MagicMock cache methods are truthy by default and would be mistaken
        # for real geocoding records by the wx capability.
        bot.db_manager.get_cached_geocoding = Mock(return_value=(None, None))
        bot.db_manager.get_cached_json = Mock(return_value=None)
        bot.db_manager.cache_geocoding = Mock()
        bot.db_manager.cache_json = Mock()
        bot.command_manager.monitor_channels = ["test"]
        bot.command_manager.commands = {c.name: c(bot) for c in (
            AskCommand, MeshCommand, LlmCommand, TestCommand, PathCommand, WxCommand,
        )}
        assert not bot.command_manager.commands["test"].can_execute(
            MeshMessage(content="test", channel="test", sender_id="DirectTest")
        )
        output = []
        async def capture(message, text, **kw):
            if message.capture_sink is not None:
                message.capture_sink.append(text)
            else:
                output.append(text)
            return True
        async def chunks(message, pages, **kw):
            for page in pages:
                await capture(message, page)
            return True
        bot.command_manager.send_response = AsyncMock(side_effect=capture)
        bot.command_manager.send_response_chunked = AsyncMock(side_effect=chunks)
        questions = ["baliz aide", "baliz tu me reçois", "baliz path"]
        if args.live:
            questions += ["baliz mesh combien de répéteurs sont dans la base ?",
                          "baliz wiki " + ("comment choisir la région radio pour un Companion ?" if args.wiki_index else "quelle est la configuration demo_region du banc de test Baliz ?"),
                          "baliz llm dis bonjour en une courte phrase"]
            questions += ["baliz quelle météo demain à Brest ?"]
            questions += ["baliz météo à Brest aujourd’hui ?"]
            if args.wiki_index:
                questions += [
                    "baliz qui es-tu ?",
                    "baliz quel est le chemin entre toi et moi ?",
                    "baliz donne moi le chemin",
                    "baliz donne moi les régions pour un Companion",
                    "baliz comment ajouter les régions à un compagnon ?",
                    "baliz comment ajouter les régions à un répéteur ?",
                ]
        report = []
        for number, question in enumerate(questions):
            output.clear()
            message = MeshMessage(content=question, channel="test", sender_id=f"Fixture{number}",
                                  snr=7.5, rssi=-92, hops=0)
            ask = bot.command_manager.commands["ask"]
            assert ask.matches_keyword(message)
            # Each fixture is a different sender so target per-user cooldowns apply naturally.
            success = await ask.execute(message)
            text = "\n".join(output)
            failures = ("indisponible", "non autorisée", "Aucune source", "dépassé le délai", "LLM error", "LLM unavailable", "No response from AI", "n’a pas pu", "Could not generate")
            passed = bool(success and text and not any(marker in text for marker in failures))
            if "combien de répéteurs" in question:
                passed = passed and "2" in text
            if "reçois" in question:
                passed = passed and ("7.5" in text or "7,5" in text)
            if "régions" in question:
                passed = passed and len(output) <= 2 and not any(
                    marker in text for marker in ("```", "||", "▼", "**")
                )
            if "ajouter les régions à un répéteur" in question:
                passed = passed and all(command in text for command in (
                    "region def", "region default", "region save",
                ))
            if "météo demain à Brest" in question or "météo à Brest aujourd’hui" in question:
                passed = passed and "brest" in text.casefold()
            report.append({"question": question, "route": ask.dispatcher.router.decide(question.split(" ", 1)[1]).route.value,
                           "passed": passed, "answer": text, "pages": len(output)})
        print(json.dumps({"radio_started": False, "live_llm": args.live, "results": report}, ensure_ascii=False, indent=2))
        return 0 if all(r["passed"] for r in report) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Use the configured LLM for mesh/Wiki/conversation")
    parser.add_argument("--endpoint", default="http://127.0.0.1:11434/v1/chat/completions")
    parser.add_argument("--model", default="gemma3:4b")
    parser.add_argument("--reasoning-effort", default="", help="Optional OpenAI-compatible reasoning effort")
    parser.add_argument("--timeout", type=int, choices=range(1, 301), metavar="SECONDS", default=180,
                        help="Per-call model timeout, 1-300 seconds; CPU-only models may need a cold start")
    parser.add_argument("--wiki-index", type=Path)
    raise SystemExit(asyncio.run(run(parser.parse_args())))
