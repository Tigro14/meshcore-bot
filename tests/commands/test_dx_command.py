"""Tests for the dx command (modules/commands/dx_command.py).

The command reads only ``observed_paths`` and ``complete_contact_tracking``, so
these tests use a real SQLite database: the SQL (entry-relay match, recency
window, zero-hop exclusion) is what carries the DX claim, and a mocked
``execute_query`` would only restate the test.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import pytest

from modules.commands.dx_command import _MIN_NAME_LEN, DxCommand
from modules.db_manager import DBManager
from modules.utils import calculate_distance
from tests.conftest import mock_message

BOT_LAT = 48.8566
BOT_LON = 2.3522

# Repeaters: one 5 km from the bot, one ~430 km away.
NEAR_REPEATER = ("a1b2c3d4" + "0" * 56, "Rep-Proche", 48.9000, 2.4000)
FAR_REPEATER = ("b2c3d4e5" + "1" * 56, "Rep-Lointain", 45.0000, 3.0000)


def _stamp(days_ago: float = 0.0) -> str:
    """A timestamp in the shape the recorder writes (naive, space separated)."""
    return (datetime.now() - timedelta(days=days_ago)).isoformat(sep=" ", timespec="seconds")


def _add_contact(
    db: DBManager,
    public_key: str,
    name: str,
    role: str,
    lat: float | None,
    lon: float | None,
    *,
    days_ago: float = 0.0,
    is_starred: int = 0,
) -> None:
    db.execute_update(
        """
        INSERT INTO complete_contact_tracking
            (public_key, name, role, first_heard, last_heard, latitude, longitude,
             last_advert_timestamp, is_starred)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (public_key, name, role, _stamp(days_ago), _stamp(days_ago), lat, lon,
         _stamp(days_ago), is_starred),
    )


def _add_advert_path(
    db: DBManager,
    *,
    author_key: str,
    from_prefix: str,
    to_prefix: str,
    path_length: int = 1,
    bytes_per_hop: int = 1,
    days_ago: float = 0.0,
) -> None:
    db.execute_update(
        """
        INSERT INTO observed_paths
            (public_key, packet_hash, from_prefix, to_prefix, path_hex, path_length,
             bytes_per_hop, packet_type, first_seen, last_seen, observation_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'advert', ?, ?, 1)
        """,
        (author_key, f"hash-{author_key}-{from_prefix}-{days_ago}", from_prefix, to_prefix,
         from_prefix + to_prefix, path_length, bytes_per_hop, _stamp(days_ago),
         _stamp(days_ago)),
    )


@pytest.fixture
def dx_bot(command_mock_bot_with_db):
    """Command bot backed by a real database holding the mesh's contacts and paths."""
    db_path = command_mock_bot_with_db.db_manager.db_path
    db = DBManager(command_mock_bot_with_db, db_path)
    command_mock_bot_with_db.db_manager = db

    db.create_table(
        "complete_contact_tracking",
        """
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        public_key TEXT NOT NULL,
        name TEXT NOT NULL,
        role TEXT NOT NULL,
        device_type TEXT,
        first_heard TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_heard TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        advert_count INTEGER DEFAULT 1,
        latitude REAL,
        longitude REAL,
        city TEXT,
        state TEXT,
        country TEXT,
        raw_advert_data TEXT,
        signal_strength REAL,
        snr REAL,
        hop_count INTEGER,
        is_currently_tracked BOOLEAN DEFAULT 0,
        last_advert_timestamp TIMESTAMP,
        location_accuracy REAL,
        contact_source TEXT DEFAULT 'advertisement',
        out_path TEXT,
        out_path_len INTEGER,
        out_bytes_per_hop INTEGER,
        is_starred INTEGER DEFAULT 0
        """,
    )
    db.create_table(
        "observed_paths",
        """
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        public_key TEXT,
        packet_hash TEXT,
        from_prefix TEXT NOT NULL,
        to_prefix TEXT NOT NULL,
        path_hex TEXT NOT NULL,
        path_length INTEGER NOT NULL,
        bytes_per_hop INTEGER,
        packet_type TEXT NOT NULL,
        first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        observation_count INTEGER DEFAULT 1
        """,
    )

    # A MagicMock ``meshcore`` makes ``get_max_message_length`` read a Mock name as the
    # radio username, which would shrink the frame to nonsense: keep it unset.
    command_mock_bot_with_db.meshcore = None
    command_mock_bot_with_db.config.set("Bot", "bot_latitude", str(BOT_LAT))
    command_mock_bot_with_db.config.set("Bot", "bot_longitude", str(BOT_LON))
    _add_contact(db, NEAR_REPEATER[0], NEAR_REPEATER[1], "repeater",
                 NEAR_REPEATER[2], NEAR_REPEATER[3])
    _add_contact(db, FAR_REPEATER[0], FAR_REPEATER[1], "repeater",
                 FAR_REPEATER[2], FAR_REPEATER[3])
    return command_mock_bot_with_db


async def _run(bot, content: str = "dx") -> str:
    """Run the command and return the single reply it sent."""
    command = DxCommand(bot)
    sent: list[str] = []

    async def capture(message, content_body, **kwargs):
        sent.append(content_body)
        return True

    bot.command_manager.send_response = capture
    message = mock_message(content=content, sender_pubkey="f" * 64)
    assert await command.execute(message) is True
    assert len(sent) == 1
    return sent[0]


class TestFarthestStationOfNearestRepeater:
    async def test_reports_far_station_of_nearest_repeater(self, dx_bot):
        """A station 450 km from the near repeater beats one 900 km from the far one."""
        db = dx_bot.db_manager
        _add_contact(db, "c" * 64, "Station-Belgique", "companion", 53.0000, 2.4000)
        _add_contact(db, "d" * 64, "Station-Maroc", "companion", 34.0000, 3.0000)
        _add_advert_path(db, author_key="c" * 64, from_prefix="a1", to_prefix="b2")
        _add_advert_path(db, author_key="d" * 64, from_prefix="b2", to_prefix="a1")

        reply = await _run(dx_bot)

        assert "Rep-Proche" in reply
        assert "Station-Belgique" in reply
        assert "Station-Maroc" not in reply

    async def test_ranks_by_repeater_to_station_distance(self, dx_bot):
        """Rows are ordered farthest first, and the farthest is the record."""
        db = dx_bot.db_manager
        _add_contact(db, "a" * 64, "Station-Proche", "companion", 49.5000, 2.4000)
        _add_contact(db, "b" * 64, "Station-Lointaine", "companion", 55.0000, 2.4000)
        _add_advert_path(db, author_key="a" * 64, from_prefix="a1", to_prefix="b2")
        _add_advert_path(db, author_key="b" * 64, from_prefix="a1", to_prefix="b2")

        reply = await _run(dx_bot)
        lines = [line for line in reply.splitlines() if line.startswith(("1.", "2."))]

        assert len(lines) == 2
        assert lines[0].startswith("1. Station-Lointaine")
        assert lines[1].startswith("2. Station-Proche")
        first_km = float(re.search(r"(\d+) km", lines[0]).group(1))
        second_km = float(re.search(r"(\d+) km", lines[1]).group(1))
        assert first_km > second_km

    async def test_relay_without_position_is_not_the_nearest(self, dx_bot):
        """A repeater with no GPS cannot be used to answer, even if it is closer."""
        db = dx_bot.db_manager
        _add_contact(db, "e1" + "0" * 62, "Rep-Sans-GPS", "repeater", None, None)
        _add_contact(db, "9" * 64, "Station-X", "companion", 53.0000, 2.4000)
        _add_advert_path(db, author_key="9" * 64, from_prefix="e1", to_prefix="a1")

        reply = await _run(dx_bot)

        assert "Rep-Proche" in reply
        assert "Station-X" not in reply


class TestEvidenceRules:
    async def test_zero_hop_reception_is_the_bots_own_dx(self, dx_bot):
        """A station heard directly by the bot is not credited to the repeater."""
        db = dx_bot.db_manager
        _add_contact(db, "7" * 64, "Station-Directe", "companion", 53.0000, 2.4000)
        _add_advert_path(db, author_key="7" * 64, from_prefix="77", to_prefix="77",
                         path_length=0)

        reply = await _run(dx_bot)

        assert "Aucune station reçue" in reply
        assert "Station-Directe" not in reply

    async def test_rows_older_than_the_window_are_dropped(self, dx_bot):
        db = dx_bot.db_manager
        _add_contact(db, "6" * 64, "Station-Vieille", "companion", 53.0000, 2.4000)
        _add_advert_path(db, author_key="6" * 64, from_prefix="a1", to_prefix="b2",
                         days_ago=30)

        default = await _run(dx_bot)
        assert "Aucune station reçue" in default

        wider = await _run(dx_bot, "dx 5 40d")
        assert "Station-Vieille" in wider
        assert "40 j" in wider

    async def test_ambiguous_prefix_belongs_to_the_nearest_owner(self, dx_bot):
        """With a shared prefix, the plausible owner is the one near the entry point."""
        db = dx_bot.db_manager
        # A second repeater sharing the "a1" prefix, far away.
        _add_contact(db, "a1" + "9" * 62, "Rep-Jumeau", "repeater", 44.0000, 1.0000)
        _add_contact(db, "5" * 64, "Station-Loin", "companion", 41.0000, 1.0000)
        _add_advert_path(db, author_key="5" * 64, from_prefix="a1", to_prefix="b2")

        reply = await _run(dx_bot)

        # The mesh graph's rule decides which twin owns "a1": the one nearest the entry
        # point, i.e. the near repeater. So the reception counts for it, and the distance
        # is measured from it, not from the far twin that shares the prefix.
        expected_km = calculate_distance(NEAR_REPEATER[2], NEAR_REPEATER[3], 41.0, 1.0)
        other_km = calculate_distance(44.0, 1.0, 41.0, 1.0)

        assert "Rep-Proche" in reply
        assert DxCommand._distance_text(expected_km) in reply
        assert DxCommand._distance_text(other_km) not in reply

    async def test_message_rows_are_not_adverts(self, dx_bot):
        """Only adverts prove who was heard; a message row proves nothing about position."""
        db = dx_bot.db_manager
        db.execute_update(
            """
            INSERT INTO observed_paths
                (public_key, packet_hash, from_prefix, to_prefix, path_hex, path_length,
                 bytes_per_hop, packet_type, first_seen, last_seen, observation_count)
            VALUES (NULL, 'h-msg', 'a1', 'b2', 'a1b2', 1, 1, 'message', ?, ?, 1)
            """,
            (_stamp(), _stamp()),
        )

        reply = await _run(dx_bot)

        assert "Aucune station reçue" in reply


class TestFormatting:
    def test_distance_units_follow_the_magnitude(self):
        """Metres under a kilometre, one decimal under ten, whole kilometres above."""
        assert DxCommand._distance_text(0.017) == "17 m"
        assert DxCommand._distance_text(6.4) == "6.4 km"
        assert DxCommand._distance_text(27.4) == "27 km"

    def test_names_shrink_before_rows_are_dropped(self):
        """A tight frame pays with the names first: the requested rows stay while they fit."""
        repeater = {"name": "R", "distance_km": 4.0}
        stations = [
            {"name": f"Station-{index}-long-nom", "distance_km": 100.0 - index, "last_seen": None}
            for index in range(5)
        ]

        roomy = DxCommand._format(DxCommand.__new__(DxCommand), repeater, stations, 5, 7, 400)
        tight = DxCommand._format(DxCommand.__new__(DxCommand), repeater, stations, 5, 7, 150)

        assert roomy.count("\n") == 5
        assert "Station-0-long-nom" in roomy
        assert len(tight.encode("utf-8")) <= 150
        assert tight.count("\n") == 5
        assert "Station-0-long-nom" not in tight
        assert all(len(line.split(". ", 1)[1].split(" ")[0]) >= _MIN_NAME_LEN
                   for line in tight.splitlines()[1:])

    def test_rows_are_dropped_least_far_first_when_names_run_out(self):
        """A frame too small for the names keeps the record and drops the tail."""
        repeater = {"name": "R", "distance_km": 4.0}
        stations = [
            {"name": f"Station-{index}-long-nom", "distance_km": 100.0 - index, "last_seen": None}
            for index in range(5)
        ]

        cramped = DxCommand._format(DxCommand.__new__(DxCommand), repeater, stations, 5, 7, 100)
        rows = cramped.splitlines()[1:]

        assert len(cramped.encode("utf-8")) <= 100
        assert len(rows) < 5
        assert rows[0].startswith("1. ")
        assert "100 km" in cramped
        assert "96 km" not in cramped
        assert "Station-0-long" in cramped

    def test_the_last_resort_row_still_respects_the_frame(self):
        """No header, one row: even a 14-byte budget is answered, not dropped."""
        repeater = {"name": "Repeater-Nom-Tres-Long-Pour-Le-Test", "distance_km": 4.0}
        stations = [{"name": "Station-Lointaine", "distance_km": 678.0, "last_seen": None}]

        reply = DxCommand._format(DxCommand.__new__(DxCommand), repeater, stations, 5, 7, 14)

        assert len(reply.encode("utf-8")) <= 14
        assert reply == "1. Stat 678 km"

    def test_a_header_too_long_for_the_frame_is_dropped_not_the_record(self):
        """A long repeater name costs the header, never the farthest station."""
        repeater = {"name": "Repeater-Nom-Tres-Long-Pour-Le-Test", "distance_km": 4.0}
        stations = [{"name": "Station-Lointaine", "distance_km": 678.0, "last_seen": None}]

        reply = DxCommand._format(DxCommand.__new__(DxCommand), repeater, stations, 5, 7, 40)

        assert len(reply.encode("utf-8")) <= 40
        assert reply == "1. Station-Lointaine 678 km"


class TestReplies:
    async def test_missing_position_explains_the_config_key(self, dx_bot):
        dx_bot.config.remove_option("Bot", "bot_latitude")
        dx_bot.config.remove_option("Bot", "bot_longitude")

        reply = await _run(dx_bot, "dx")

        assert "Position inconnue" in reply
        assert "bot_latitude" in reply

    async def test_no_repeater_with_position(self, dx_bot):
        dx_bot.db_manager.execute_update(
            "UPDATE complete_contact_tracking SET latitude = NULL, longitude = NULL"
        )

        reply = await _run(dx_bot)

        assert "Aucun répéteur" in reply

    async def test_sender_position_is_the_fallback_reference(self, dx_bot):
        """No bot GPS: measure "nearest" from the sender's own advertised position."""
        db = dx_bot.db_manager
        dx_bot.config.remove_option("Bot", "bot_latitude")
        dx_bot.config.remove_option("Bot", "bot_longitude")
        sender_key = "s" * 64
        _add_contact(db, sender_key, "Requester", "companion", 45.0000, 3.0000)
        _add_contact(db, "f" * 64, "Station-Alger", "companion", 36.7500, 3.0500)
        _add_advert_path(db, author_key="f" * 64, from_prefix="b2", to_prefix="a1")

        command = DxCommand(dx_bot)
        sent: list[str] = []

        async def capture(message, content_body, **kwargs):
            sent.append(content_body)
            return True

        dx_bot.command_manager.send_response = capture
        message = mock_message(content="dx", sender_pubkey=sender_key)
        assert await command.execute(message) is True

        assert "Rep-Lointain" in sent[0]
        assert "Station-Alger" in sent[0]

    async def test_no_stations_names_the_repeater(self, dx_bot):
        reply = await _run(dx_bot)

        assert "Aucune station reçue" in reply
        assert "Rep-Proche" in reply
        assert "7 derniers jours" in reply


class TestArgumentsAndBudget:
    async def test_parse_args_defaults_and_overrides(self, dx_bot):
        command = DxCommand(dx_bot)

        assert command._parse_args(mock_message(content="dx")) == (5, 7)
        assert command._parse_args(mock_message(content="dx 3")) == (3, 7)
        assert command._parse_args(mock_message(content="dx 5 30d")) == (5, 30)
        assert command._parse_args(mock_message(content="dx 99 999d")) == (10, 365)

    async def test_parse_args_honours_the_command_prefix(self, dx_bot):
        dx_bot.config.set("Bot", "command_prefix", "!")
        command = DxCommand(dx_bot)

        assert command._parse_args(mock_message(content="!dx 4 15d")) == (4, 15)

    async def test_config_sections_drive_defaults(self, dx_bot):
        dx_bot.config.add_section("Dx_Command")
        dx_bot.config.set("Dx_Command", "limit", "2")
        dx_bot.config.set("Dx_Command", "days", "14")
        command = DxCommand(dx_bot)

        assert command._parse_args(mock_message(content="dx")) == (2, 14)

    async def test_disabled_command_does_not_execute(self, dx_bot):
        dx_bot.config.add_section("Dx_Command")
        dx_bot.config.set("Dx_Command", "enabled", "false")
        command = DxCommand(dx_bot)

        assert command.can_execute(mock_message(content="dx")) is False

    async def test_reply_stays_inside_the_frame(self, dx_bot):
        """A crowded top 10 with long names must still fit one LoRa frame."""
        db = dx_bot.db_manager
        for index in range(12):
            key = f"{index:02d}" + "0" * 62
            _add_contact(db, key, f"Station-{index}-tres-longue-nom-complet", "companion",
                         49.0 + index * 0.5, 2.4)
            _add_advert_path(db, author_key=key, from_prefix="a1", to_prefix="b2")

        bot_name_len = len(dx_bot.config.get("Bot", "bot_name").encode("utf-8"))
        budget = 155 - bot_name_len - 2

        command = DxCommand(dx_bot)
        sent: list[str] = []

        async def capture(message, content_body, **kwargs):
            sent.append(content_body)
            return True

        dx_bot.command_manager.send_response = capture
        message = mock_message(content="dx 10", sender_pubkey="f" * 64)
        assert await command.execute(message) is True

        reply = sent[0]
        assert len(reply.encode("utf-8")) <= budget
        assert len([line for line in reply.splitlines() if line.startswith("1.")]) == 1
