"""Tests for modules.channel_occupancy — the airtime estimate, the windows, the buckets.

The checks here are about what the numbers are allowed to claim: an estimate is not a
measurement, a gap is not a zero, and one node's hearing is not the channel.
"""

from __future__ import annotations

import configparser
import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from modules import channel_occupancy as occupancy_module
from modules.channel_occupancy import (
    LIVE_METADATA_KEY,
    ChannelOccupancy,
    lora_airtime_ms,
    nearest_rank_percentile,
    normalize_bw_khz,
    normalize_cr_den,
    render_key_value,
)
from modules.db_migrations import MigrationRunner
from modules.web_viewer.cleanup import CleanupSchedulerMixin
from modules.web_viewer.dashboard import DashboardSnapshotMixin

# ---------------------------------------------------------------------------
# The airtime formula
# ---------------------------------------------------------------------------


class TestAirtimeFormula:
    def test_matches_published_reference_explicit_header(self):
        """SF12/125 kHz/CR 4/5, PL 64, explicit header, DE on, preamble 8.

        The reference value comes from an independent published calculator, not from
        this implementation: agreeing with itself is not a test.
        """
        assert round(lora_airtime_ms(64, sf=12, bw_khz=125, cr_den=5, preamble=8), 3) == 2793.472

    def test_matches_published_reference_implicit_header(self):
        """SF12/500 kHz, PL 8, CR 4/5, implicit header, preamble 6, DE off."""
        assert round(lora_airtime_ms(
            8, sf=12, bw_khz=500, cr_den=5, preamble=6, explicit_header=False, ldra=False
        ), 3) == 190.464

    def test_coding_rate_multiplies_the_symbol_count(self):
        """The coding rate is the denominator, so CR 4/8 costs more airtime than CR 4/5."""
        base = lora_airtime_ms(30, sf=9, bw_khz=125, cr_den=5, preamble=8, ldra=False)
        higher = lora_airtime_ms(30, sf=9, bw_khz=125, cr_den=8, preamble=8, ldra=False)
        # The 8 fixed symbols are not multiplied by the coding rate, so the ratio is
        # below 8/5 — and it must not be the ratio a wrong reading of the rate gives.
        assert base < higher < base * 8 / 5

    def test_crc_and_header_mode_change_the_coded_bits(self):
        # Chosen so the extra bits cross a symbol boundary. At SF9 with DE off a symbol
        # carries 36 bits, so 16 CRC bits on a 20-byte payload do not move the count at
        # all — that is why this case is SF6, where a symbol carries 24 bits.
        symbol_ms = 2.0 ** 6 / 125
        with_crc = lora_airtime_ms(5, sf=6, ldra=False)
        without_crc = lora_airtime_ms(5, sf=6, crc=False, ldra=False)
        implicit_header = lora_airtime_ms(5, sf=6, explicit_header=False, ldra=False)
        assert without_crc < with_crc
        assert implicit_header < with_crc
        # One extra coded symbol, multiplied by the coding rate.
        assert round(with_crc - without_crc, 6) == round(5 * symbol_ms, 6)
        assert round(with_crc - implicit_header, 6) == round(5 * symbol_ms, 6)

    def test_zero_preamble_still_carries_the_sync_pattern(self):
        """4.25 symbols of sync exist even with a zero-length preamble."""
        symbol_ms = 2.0 ** 12 / 125
        assert lora_airtime_ms(1, sf=12, preamble=0) > 4 * symbol_ms

    def test_low_data_rate_auto_rule_applies_above_16_38_ms(self):
        """SF12/125 kHz has a 32.768 ms symbol time, so DE is on by the datasheet rule.

        DE on means fewer bits per symbol, which means more symbols for the same payload:
        the rule costs airtime on purpose, because a receiver cannot decode a symbol that
        carries too few bits.
        """
        auto_at_sf12 = lora_airtime_ms(64, sf=12, bw_khz=125, cr_den=5, preamble=8)
        assert auto_at_sf12 == lora_airtime_ms(64, sf=12, bw_khz=125, cr_den=5, preamble=8, ldra=True)
        assert auto_at_sf12 > lora_airtime_ms(64, sf=12, bw_khz=125, cr_den=5, preamble=8, ldra=False)
        # Below the threshold the rule does not apply, so the auto value is DE off.
        assert lora_airtime_ms(64, sf=7, bw_khz=125, cr_den=5, preamble=8) == lora_airtime_ms(
            64, sf=7, bw_khz=125, cr_den=5, preamble=8, ldra=False
        )

    def test_spreading_factor_is_clamped_to_the_supported_range(self):
        """SF 0 is not a spreading factor: an airtime built from it is not small, it is wrong."""
        assert lora_airtime_ms(20, sf=0) == lora_airtime_ms(20, sf=6)
        assert lora_airtime_ms(20, sf=99) == lora_airtime_ms(20, sf=12)


class TestNormalization:
    def test_bandwidth_accepts_hz_and_khz(self):
        assert normalize_bw_khz(125) == 125.0
        assert normalize_bw_khz(125000) == 125.0
        # A garbage value falls back to the default rather than becoming an airtime of 0.
        assert normalize_bw_khz("not a number") == 125.0
        assert normalize_bw_khz(0) == 125.0

    def test_coding_rate_accepts_both_notations(self):
        assert normalize_cr_den(7) == 7      # denominator notation (4/7)
        assert normalize_cr_den(3) == 7      # AN1200.13 index notation (4/7)
        assert normalize_cr_den(1) == 5
        assert normalize_cr_den(4) == 8
        assert normalize_cr_den(0) == 5
        assert normalize_cr_den("4/5") == 5


class TestNearestRankPercentile:
    def test_nearest_rank_does_not_interpolate_the_tail(self):
        values = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0]
        # Linear interpolation would report 57.5 here, which understates the burst —
        # and the burst is the reason anyone looks at a p95.
        assert nearest_rank_percentile(values, 0.95) == 60.0
        assert nearest_rank_percentile(values, 0.5) == 30.0

    def test_no_samples_is_not_zero(self):
        assert nearest_rank_percentile([], 0.95) is None

    def test_quantile_is_clamped(self):
        assert nearest_rank_percentile([5.0, 7.0], 1.0) == 7.0
        assert nearest_rank_percentile([5.0, 7.0], 0.0) == 5.0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config():
    parser = configparser.ConfigParser()
    parser.add_section("Channel_Occupancy")
    parser.set("Channel_Occupancy", "enabled", "true")
    parser.set("Channel_Occupancy", "sample_seconds", "60")
    parser.set("Channel_Occupancy", "window_seconds", "60")
    return parser


@pytest.fixture
def occupancy(config):
    return ChannelOccupancy(config, logging.getLogger("test_channel_occupancy"))


@pytest.fixture
def conn(occupancy, tmp_path):
    """A database file with the tables migration 25 creates — the same file the viewer reads."""
    connection = sqlite3.connect(str(tmp_path / "meshcore_bot.db"))
    connection.row_factory = sqlite3.Row
    MigrationRunner(connection, logging.getLogger("test_migrations")).run()
    yield connection
    connection.close()


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


class TestRecording:
    def test_rx_and_tx_are_never_merged(self, occupancy):
        occupancy.record(30, kind="rx")
        occupancy.record(120, kind="tx", sender="bot")
        fields = occupancy.fields()
        assert fields["window_rx_ms"] < fields["window_tx_ms"]
        assert "window_rx_percent" in fields and "window_tx_percent" in fields
        # No field sums the two shares: there is no single airtime number.
        assert not any("rx" in key and "tx" in key for key in fields)

    def test_window_clock_is_monotonic(self, occupancy):
        """A wall-clock jump must not stretch or collapse a window.

        The window is what a reader sees as "the last 10 seconds", so it has to be tied
        to a clock that cannot be corrected by NTP or by a clock-sync command.
        """
        monotonic = {"now": 1000.0}
        wall = {"now": 1_700_000_000.0}
        with patch.object(occupancy_module.time, "monotonic", side_effect=lambda: monotonic["now"]), \
             patch.object(occupancy_module.time, "time", side_effect=lambda: wall["now"]):
            occupancy.record(30, kind="rx")
            # The wall clock jumps by 1000 s while the monotonic clock moves 4 s.
            wall["now"] += 1000.0
            monotonic["now"] += 4.0
            assert occupancy.window_airtime(10.0)["samples"] == 1
            monotonic["now"] += 7.0
            assert occupancy.window_airtime(10.0)["samples"] == 0

    def test_ring_buffer_is_bounded(self, config):
        config.set("Channel_Occupancy", "max_samples", "10")
        bounded = ChannelOccupancy(config, logging.getLogger("test_channel_occupancy"))
        for _ in range(50):
            bounded.record(20, kind="rx")
        assert len(bounded._samples) == 10

    def test_device_modulation_wins_over_the_configured_one(self, occupancy):
        occupancy.set_device_modulation({"sf": 11, "bw": 125, "cr": 8})
        assert occupancy.modulation()["sf"] == 11
        assert occupancy.modulation()["cr_den"] == 8

    def test_unknown_device_modulation_keeps_the_last_known_value(self, occupancy):
        occupancy.set_device_modulation({"sf": 11, "bw": 250, "cr": 6})
        occupancy.refresh_from_device({"radio_sf": 0, "radio_bw": 0, "radio_cr": 0})
        assert occupancy.modulation()["sf"] == 11
        assert occupancy.modulation()["bw_khz"] == 250.0

    def test_refresh_reads_a_device_object_or_a_dict(self, occupancy):
        class DeviceInfo:
            radio_sf = 10
            radio_bw = 500
            radio_cr = 7

        assert occupancy.refresh_from_device(DeviceInfo())
        assert occupancy.modulation()["sf"] == 10
        assert occupancy.modulation()["bw_khz"] == 500.0
        assert occupancy.modulation()["cr_den"] == 7
        assert not occupancy.refresh_from_device(None)

    def test_default_is_off(self):
        quiet = ChannelOccupancy(configparser.ConfigParser(), logging.getLogger("test"))
        assert quiet.enabled is False
        assert quiet.fields()["enabled"] is False


# ---------------------------------------------------------------------------
# Buckets, storage, retention
# ---------------------------------------------------------------------------


class TestBucketsAndStorage:
    def test_quiet_bucket_stays_absent(self, occupancy, conn):
        occupancy.record(30, kind="rx", at=1_700_000_000.0)
        occupancy.flush(conn)
        rows = conn.execute("SELECT bucket_ts, packets, rx_ms FROM occupancy_samples").fetchall()
        assert len(rows) == 1
        assert rows[0]["packets"] == 1
        # Nothing is written for a bucket with no packet, so absence stays the reader's gap.
        assert conn.execute("SELECT COUNT(*) FROM occupancy_samples WHERE rx_ms = 0").fetchone()[0] == 0

    def test_history_keeps_the_gap_and_the_real_zero_apart(self, occupancy, conn):
        base = int(occupancy_module.time.time()) // 60 * 60
        occupancy.record(30, kind="rx", at=base)
        occupancy.flush(conn)
        # A bucket observed with nothing on air is a fact; the reader must not collapse it
        # into the gap that precedes it.
        conn.execute(
            "INSERT INTO occupancy_samples (bucket_ts, sample_seconds, rx_ms, tx_ms, packets, rx_bytes) "
            "VALUES (?, 60, 0.0, 0.0, 0, 0)",
            (base + 60,),
        )
        conn.commit()
        buckets = occupancy.history(conn, base, base + 180.0)["buckets"]
        assert len(buckets) == 2  # base + 120 was never written and stays absent
        assert buckets[0]["rx_ms"] > 0.0
        assert buckets[1]["rx_ms"] == 0.0

    def test_history_stores_sums_and_counts_not_means(self, occupancy, conn):
        base = int(occupancy_module.time.time()) // 60 * 60
        for offset in (0, 30):
            occupancy.record(40, kind="rx", at=base + offset)
        occupancy.flush(conn)
        payload = occupancy.history(conn, base, base + 10000.0)
        assert payload["packets"] == 2
        # The total is the sum of the stored sums, rounded for the response; it is not an
        # average of anything.
        assert payload["total_rx_ms"] == pytest.approx(payload["buckets"][0]["rx_ms"], abs=0.01)
        assert not any("mean" in key for key in payload)

    def test_reflush_inside_a_bucket_is_idempotent(self, occupancy, conn):
        occupancy.record(30, kind="rx", at=1_700_000_000.0)
        first = occupancy.flush(conn)
        occupancy.record(20, kind="rx", at=1_700_000_010.0)
        second = occupancy.flush(conn)
        assert second["packets"] >= first["packets"]
        row = conn.execute("SELECT packets, rx_ms FROM occupancy_samples").fetchone()
        assert row["packets"] == 2

    def test_sender_share_counts_packets_not_flushes(self, occupancy, conn):
        occupancy.record(30, kind="rx", sender="alice", at=1_700_000_000.0)
        occupancy.flush(conn)
        occupancy.record(20, kind="rx", sender="alice", at=1_700_000_060.0)
        occupancy.flush(conn)
        row = conn.execute("SELECT packets, airtime_ms FROM occupancy_senders").fetchone()
        assert row["packets"] == 2

    def test_senders_ranked_by_airtime_not_by_packet_count(self, occupancy, conn):
        # Bob sends three small packets, Alice one long one. On air Alice can still cost
        # more, which is exactly what a fairness limit would have to be built on.
        occupancy.record(150, kind="rx", sender="alice", at=1_700_000_000.0)
        for offset in (0, 1, 2):
            occupancy.record(5, kind="rx", sender="bob", at=1_700_000_060.0 + offset)
        occupancy.flush(conn)
        assert occupancy.top_senders(conn, limit=5)[0]["sender"] == "alice"

    def test_live_snapshot_carries_its_age(self, occupancy, conn):
        occupancy.record(30, kind="rx", sender="alice", at=1_700_000_000.0)
        occupancy.flush(conn)
        snapshot = occupancy.read_live(conn)
        assert snapshot["stale"] is False
        assert snapshot["age_seconds"] is not None
        assert snapshot["fields"]["samples"] == 1

    def test_no_snapshot_is_not_reported_as_zero(self, occupancy, conn):
        snapshot = occupancy.read_live(conn)
        assert snapshot["stale"] is True
        assert snapshot["age_seconds"] is None
        assert snapshot["fields"] == {}

    def test_stale_after_two_bucket_lengths(self, occupancy, conn):
        conn.execute(
            "INSERT INTO bot_metadata (key, value, updated_at) VALUES (?, ?, ?)",
            (LIVE_METADATA_KEY, "{}", 1_700_000_000.0),
        )
        conn.commit()
        snapshot = occupancy.read_live(conn)
        assert snapshot["stale"] is True
        assert snapshot["age_seconds"] > 120.0

    def test_key_value_output_is_parseable(self, occupancy):
        occupancy.record(30, kind="rx", sender="alice", at=1_700_000_000.0)
        lines = render_key_value(occupancy.fields()).splitlines()
        assert lines and all("=" in line for line in lines)
        parsed = dict(line.split("=", 1) for line in lines)
        assert parsed["enabled"] == "1"
        assert parsed["samples"] == "1"


class TestRetention:
    """Retention lives in [Data_Retention] and is applied by the viewer's cleanup scheduler."""

    class Stub(CleanupSchedulerMixin, DashboardSnapshotMixin):
        """Prunes through the same helper the scheduler uses, on a connection we keep open.

        The helper opens its own connection for every batch, which is the point: each
        commit releases the writer lock. The stub hands out the test connection without
        closing it, so the assertions can still read what was deleted.
        """

        def __init__(self, parser, connection):
            self.config = parser
            self.logger = logging.getLogger("test_cleanup")
            self._connection = connection

        @contextmanager
        def _with_db_connection(self):
            yield self._connection

    @pytest.fixture
    def retention_config(self):
        parser = configparser.ConfigParser()
        parser.add_section("Data_Retention")
        parser.set("Data_Retention", "occupancy_samples_retention_days", "30")
        parser.set("Data_Retention", "occupancy_senders_retention_days", "90")
        return parser

    def test_buckets_past_retention_are_pruned(self, retention_config, conn):
        old_bucket = int(occupancy_module.time.time()) - 31 * 86400
        conn.execute(
            "INSERT INTO occupancy_samples (bucket_ts, sample_seconds, rx_ms, tx_ms, packets, rx_bytes) "
            "VALUES (?, 60, 10.0, 0.0, 1, 20)",
            (old_bucket,),
        )
        conn.execute(
            "INSERT INTO occupancy_samples (bucket_ts, sample_seconds, rx_ms, tx_ms, packets, rx_bytes) "
            "VALUES (?, 60, 10.0, 0.0, 1, 20)",
            (int(occupancy_module.time.time()) - 29 * 86400,),
        )
        conn.commit()
        self.Stub(retention_config, conn)._cleanup_occupancy()
        remaining = conn.execute("SELECT bucket_ts FROM occupancy_samples").fetchall()
        assert len(remaining) == 1
        assert remaining[0]["bucket_ts"] > old_bucket

    def test_senders_past_retention_are_pruned(self, retention_config, conn):
        conn.execute(
            "INSERT INTO occupancy_senders (sender, airtime_ms, packets, first_seen, last_seen) "
            "VALUES ('alice', 10.0, 1, ?, ?)",
            (1_700_000_000.0, 1_700_000_000.0),
        )
        conn.commit()
        self.Stub(retention_config, conn)._cleanup_occupancy()
        assert conn.execute("SELECT COUNT(*) FROM occupancy_senders").fetchone()[0] == 0

    def test_absent_tables_are_skipped_not_reported(self, retention_config):
        # An install that never enabled the feature has no occupancy tables at all.
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.Stub(retention_config, connection)._cleanup_occupancy()
        connection.close()

    def test_retention_keys_are_documented_in_the_example(self):
        example = configparser.ConfigParser()
        example.read(str(Path(__file__).resolve().parent.parent / "config.ini.example"))
        assert example.has_section("Channel_Occupancy")
        assert example.get("Channel_Occupancy", "enabled") == "false"
        assert example.has_option("Data_Retention", "occupancy_samples_retention_days")
        assert example.has_option("Data_Retention", "occupancy_senders_retention_days")


# ---------------------------------------------------------------------------
# Recording hooks
# ---------------------------------------------------------------------------


def _make_bot(occupancy_instance):
    """Minimal mock bot the integration and the transmission tracker read."""
    bot = MagicMock()
    bot.logger = Mock()
    parser = configparser.ConfigParser()
    parser.add_section("Bot")
    parser.add_section("Web_Viewer")
    bot.config = parser
    bot.bot_root = "/tmp"
    bot.db_manager = MagicMock()
    bot.db_manager.db_path = ":memory:"
    bot.occupancy = occupancy_instance
    bot.transmission_tracker = None
    return bot


def _integration(bot):
    from modules.web_viewer.integration import BotIntegration

    with patch.object(BotIntegration, "_init_http_session"), \
         patch.object(BotIntegration, "_init_packet_stream_table"):
        return BotIntegration(bot)


class TestRecordingHooks:
    def test_empty_payload_is_not_recorded(self, occupancy):
        """A packet with no payload is a decode failure, not a packet that cost nothing."""
        _integration(_make_bot(occupancy))._record_occupancy({"routing_info": {"payload_length": 0}}, "rx")
        assert occupancy.fields()["samples"] == 0

    def test_a_heard_packet_is_recorded_on_the_packet_path(self, occupancy):
        """capture_full_packet_data sees every packet, so it is where rx is recorded."""
        _integration(_make_bot(occupancy))._record_occupancy(
            {"routing_info": {"payload_length": 30}, "sender_pubkey": "alice"}, "rx"
        )
        fields = occupancy.fields()
        assert fields["samples"] == 1
        assert fields["fast_rx_ms"] > 0.0 and fields["fast_tx_ms"] == 0.0

    def test_transmission_is_recorded_as_tx_not_rx(self, occupancy):
        from modules.transmission_tracker import TransmissionTracker

        TransmissionTracker(_make_bot(occupancy)).record_transmission("hello", "general", "channel")
        fields = occupancy.fields()
        assert fields["samples"] == 1
        assert fields["fast_tx_ms"] > 0.0
        assert fields["fast_rx_ms"] == 0.0

    def test_flush_skips_absent_tables(self, occupancy):
        """The writer may point at a database this process never migrated."""
        integration = _integration(_make_bot(occupancy))
        connection = sqlite3.connect(":memory:")
        occupancy.record(30, kind="rx")
        integration._flush_occupancy(connection)
        connection.close()

    def test_nothing_is_recorded_when_disabled(self):
        quiet = ChannelOccupancy(configparser.ConfigParser(), logging.getLogger("test"))
        _integration(_make_bot(quiet))._record_occupancy({"routing_info": {"payload_length": 30}}, "rx")
        assert quiet.fields()["samples"] == 0


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@pytest.fixture
def viewer(occupancy, conn, tmp_path):
    """BotDataViewer on the same config and database the writer used."""
    from modules.web_viewer.app import BotDataViewer

    parser = configparser.ConfigParser()
    parser.add_section("Bot")
    parser.set("Bot", "db_path", str(tmp_path / "meshcore_bot.db"))
    parser.set("Bot", "bot_name", "TestBot")
    parser.add_section("Channels")
    parser.set("Channels", "monitor_channels", "general")
    parser.add_section("Channel_Occupancy")
    parser.set("Channel_Occupancy", "enabled", "true")
    parser.add_section("Web_Viewer")
    for key, value in [
        ("host", "127.0.0.1"), ("port", "8080"), ("enabled", "false"),
        ("auto_start", "false"), ("debug", "false"),
        ("cors_allowed_origins", "*"), ("web_viewer_password", "test-password"),
    ]:
        parser.set("Web_Viewer", key, value)

    config_path = str(tmp_path / "config.ini")
    with open(config_path, "w") as handle:
        parser.write(handle)

    with patch.object(BotDataViewer, "_start_database_polling"), \
         patch.object(BotDataViewer, "_start_log_tailing"), \
         patch.object(BotDataViewer, "_start_cleanup_scheduler"), \
         patch.object(BotDataViewer, "_start_dashboard_refresher"), \
         patch.object(BotDataViewer, "_setup_socketio_handlers"), \
         patch("modules.web_viewer.app.RepeaterManager"):
        instance = BotDataViewer(db_path=str(tmp_path / "meshcore_bot.db"), config_path=config_path)

    instance.app.testing = True
    return instance


def _write_snapshot(occupancy, conn):
    """Buckets are read through a wall-clock window, so the samples have to be recent."""
    base = int(occupancy_module.time.time()) // 60 * 60
    occupancy.record(40, kind="rx", sender="alice", at=base)
    occupancy.record(120, kind="tx", sender="bot", at=base + 10)
    occupancy.flush(conn)


class TestApi:
    def test_live_payload_carries_the_age_and_the_two_shares(self, occupancy, conn, viewer):
        _write_snapshot(occupancy, conn)
        payload = json.loads(viewer.app.test_client().get("/api/telemetry/occupancy").get_data())
        assert payload["enabled"] is True
        assert payload["stale"] is False
        assert payload["age_seconds"] is not None
        assert payload["window_rx_percent"] > 0.0
        # Reported apart: the API never returns a single airtime number.
        assert "window_rx_percent" in payload and "window_tx_percent" in payload
        assert not any("rx" in key and "tx" in key for key in payload)

    def test_enabled_is_the_setting_now_not_the_one_the_snapshot_was_written_under(self, occupancy, conn, viewer):
        """The page tells the reader to set [Channel_Occupancy] enabled, so it has to report
        the setting in force rather than the value a snapshot happens to carry.

        Recording may have been on when the snapshot was written and off now. Those are two
        facts: a payload that reports only the stored one tells the reader to turn on what is
        already on, or presents historical numbers as current ones.
        """
        _write_snapshot(occupancy, conn)
        viewer.occupancy.enabled = False
        payload = json.loads(viewer.app.test_client().get("/api/telemetry/occupancy").get_data())
        assert payload["enabled"] is False
        assert payload["snapshot_enabled"] is True
        # The figures still stand for the window they were recorded in.
        assert payload["window_rx_percent"] > 0.0

    def test_text_format_is_one_field_per_line(self, occupancy, conn, viewer):
        _write_snapshot(occupancy, conn)
        response = viewer.app.test_client().get("/api/telemetry/occupancy?format=text")
        assert response.mimetype == "text/plain"
        lines = response.get_data(as_text=True).splitlines()
        assert lines and all("=" in line for line in lines)
        parsed = dict(line.split("=", 1) for line in lines)
        assert parsed["enabled"] == "1"
        assert parsed["stale"] == "0"

    def test_history_keeps_buckets_absent(self, occupancy, conn, viewer):
        _write_snapshot(occupancy, conn)
        payload = json.loads(viewer.app.test_client().get("/api/telemetry/occupancy/history?days=30").get_data())
        assert len(payload["buckets"]) == 1
        assert payload["p50_rx_ms"] is not None
        assert payload["p95_rx_ms"] is not None

    def test_history_days_is_clamped(self, viewer):
        client = viewer.app.test_client()
        payload = json.loads(client.get("/api/telemetry/occupancy/history?days=99999").get_data())
        assert payload["end_time"] - payload["start_time"] <= 90 * 86400
        bad = json.loads(client.get("/api/telemetry/occupancy/history?days=abc").get_data())
        assert bad["end_time"] - bad["start_time"] == 86400

    def test_routes_are_on_the_anonymous_allowlist(self, viewer):
        client = viewer.app.test_client()
        # With a password set, a protected API route answers 401; these two must not.
        assert client.get("/api/telemetry/occupancy").status_code == 200
        assert client.get("/api/telemetry/occupancy/history").status_code == 200
        assert client.get("/api/plugins").status_code == 401
