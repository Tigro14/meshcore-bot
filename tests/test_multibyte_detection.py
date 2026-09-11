"""Tests for modules.multibyte_detection — pure logic + DB-backed helpers."""

import sqlite3
from datetime import datetime, timedelta

import pytest

from modules.multibyte_detection import (
    bucket_hop_chunks,
    chunks_from_multibyte_path_hex,
    collect_multibyte_hop_chunks,
    compute_path_encoding_badge,
    count_tracked_repeaters,
    find_onebyte_repeaters,
)


class TestChunksFromMultibytePathHex:
    def test_2byte(self):
        assert chunks_from_multibyte_path_hex("aaaa0bbb", 2) == ["aaaa", "0bbb"]

    def test_3byte(self):
        assert chunks_from_multibyte_path_hex("abcdef123456", 3) == ["abcdef", "123456"]

    def test_1byte_ignored(self):
        assert chunks_from_multibyte_path_hex("aa0b", 1) == []

    def test_trailing_partial_dropped(self):
        assert chunks_from_multibyte_path_hex("aaaa0bb", 2) == ["aaaa"]

    def test_empty(self):
        assert chunks_from_multibyte_path_hex("", 2) == []


class TestBucketHopChunks:
    def test_buckets(self):
        b = bucket_hop_chunks({"aaaa", "111111", "bb"})
        assert b[4] == {"aaaa"}
        assert b[6] == {"111111"}


class TestComputePathEncodingBadge:
    def _row(self, **kw):
        base = {
            "public_key": "1122334455667788",
            "role": "companion",
            "out_bytes_per_hop": None,
            "out_path_len": -1,
            "advert_count": 0,
        }
        base.update(kw)
        return base

    def test_multibyte_out_encoding(self):
        assert compute_path_encoding_badge(self._row(out_bytes_per_hop=2), [], set()) == "multibyte"

    def test_multibyte_path(self):
        assert compute_path_encoding_badge(self._row(), [{"bytes_per_hop": 3}], set()) == "multibyte"

    def test_multibyte_pk_prefix(self):
        row = self._row(public_key="aaaa99", role="repeater", advert_count=5)
        assert compute_path_encoding_badge(row, [], {"aaaa"}) == "multibyte"

    def test_one_byte_signal_adverts(self):
        assert compute_path_encoding_badge(self._row(advert_count=10, out_bytes_per_hop=1), [], set()) == "one_byte"

    def test_one_byte_signal_out_path_len(self):
        assert compute_path_encoding_badge(self._row(out_path_len=0), [], set()) == "one_byte"

    def test_none_no_signal(self):
        assert compute_path_encoding_badge(self._row(), [], set()) is None

    def test_mixed_paths_is_multibyte_not_one_byte(self):
        assert compute_path_encoding_badge(self._row(out_path_len=0), [{"bytes_per_hop": 2}], set()) == "multibyte"


class TestCollectMultibyteHopChunks:
    def test_collect(self, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "t.db"))
        conn.execute(
            "CREATE TABLE observed_paths (public_key TEXT, path_hex TEXT, bytes_per_hop INTEGER, packet_type TEXT, last_seen TEXT)"
        )
        conn.execute("INSERT INTO observed_paths VALUES ('k', 'aaaa0bbb', 2, 'advert', '2026-01-01')")
        conn.execute("INSERT INTO observed_paths VALUES ('k', 'abcdef', 3, 'advert', '2026-01-01')")
        conn.execute("INSERT INTO observed_paths VALUES ('k', 'aa', 1, 'advert', '2026-01-01')")
        chunks = collect_multibyte_hop_chunks(conn.cursor())
        assert chunks == {"aaaa", "0bbb", "abcdef"}
        conn.close()


def _make_db(db_path):
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute(
        """CREATE TABLE complete_contact_tracking (
        id INTEGER PRIMARY KEY AUTOINCREMENT, public_key TEXT NOT NULL, name TEXT,
        role TEXT, device_type TEXT, first_heard TEXT, last_heard TEXT,
        advert_count INTEGER DEFAULT 0, latitude REAL, longitude REAL, city TEXT,
        state TEXT, country TEXT, raw_advert_data TEXT, signal_strength REAL, snr REAL,
        hop_count INTEGER, is_currently_tracked BOOLEAN DEFAULT 0,
        last_advert_timestamp TEXT, location_accuracy REAL, contact_source TEXT,
        out_path TEXT, out_path_len INTEGER, out_bytes_per_hop INTEGER, is_starred INTEGER DEFAULT 0)"""
    )
    c.execute(
        """CREATE TABLE observed_paths (
        id INTEGER PRIMARY KEY AUTOINCREMENT, public_key TEXT, path_hex TEXT,
        path_length INTEGER, bytes_per_hop INTEGER, observation_count INTEGER,
        packet_type TEXT, last_seen TEXT)"""
    )
    conn.commit()
    return conn


def _add_repeater(conn, pk, name, role="repeater", tracked=1, obph=None, opl=0, adv=10, last=None):
    conn.execute(
        "INSERT INTO complete_contact_tracking (public_key, name, role, is_currently_tracked, out_bytes_per_hop, out_path_len, advert_count, last_heard) VALUES (?,?,?,?,?,?,?,?)",
        (pk, name, role, tracked, obph, opl, adv, last),
    )


class TestFindOnebyteRepeaters:
    @pytest.fixture
    def db(self, tmp_path):
        db_path = str(tmp_path / "mb.db")
        conn = _make_db(db_path)
        now = datetime.now()
        _add_repeater(conn, "1122334455667788", "Alpha", tracked=1, obph=1, opl=0, adv=10, last=str(now))
        _add_repeater(conn, "2222334455667788", "Bravo", tracked=1, obph=2, opl=0, adv=20, last=str(now))
        _add_repeater(conn, "3322334455667788", "Charlie", tracked=1, obph=None, opl=0, adv=5, last=str(now))
        conn.execute(
            "INSERT INTO observed_paths (public_key, path_hex, path_length, bytes_per_hop, observation_count, packet_type, last_seen) VALUES (?,?,?,?,?,?,?)",
            ("3322334455667788", "abcdef", 1, 3, 1, "advert", str(now)),
        )
        _add_repeater(conn, "4422334455667788", "Delta", tracked=0, obph=1, opl=0, adv=8, last=str(now))
        _add_repeater(
            conn,
            "5522334455667788",
            "Echo",
            role="roomserver",
            tracked=1,
            obph=None,
            opl=0,
            adv=3,
            last=str(now - timedelta(hours=2)),
        )
        conn.commit()
        yield db_path
        conn.close()

    def test_filters_and_orders(self, db):
        conn = sqlite3.connect(db)
        res = find_onebyte_repeaters(conn, top_n=5)
        conn.close()
        assert [r["name"] for r in res] == ["Alpha", "Echo"]  # Alpha most recent first

    def test_top_n(self, db):
        conn = sqlite3.connect(db)
        res = find_onebyte_repeaters(conn, top_n=1)
        conn.close()
        assert [r["name"] for r in res] == ["Alpha"]

    def test_count_tracked(self, db):
        conn = sqlite3.connect(db)
        assert count_tracked_repeaters(conn) == 4
        conn.close()


class TestFormatSeen:
    def test_zero_seconds(self):
        from modules.commands.multibyte_command import MultibyteCommand

        assert MultibyteCommand._format_seen(str(datetime.now())) == "0s ago"

    def test_hours(self):
        from modules.commands.multibyte_command import MultibyteCommand

        t = datetime.now() - timedelta(hours=3)
        assert MultibyteCommand._format_seen(str(t)) == "3h ago"

    def test_invalid_falls_back_to_raw(self):
        from modules.commands.multibyte_command import MultibyteCommand

        assert MultibyteCommand._format_seen("not-a-date") == "not-a-date"
