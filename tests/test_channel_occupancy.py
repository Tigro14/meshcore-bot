"""Tests for modules.channel_occupancy — the airtime estimate, the windows, the buckets.

The checks here are about what the numbers are allowed to claim: an estimate is not a
measurement, a gap is not a zero, and one node's hearing is not the channel.
"""

from __future__ import annotations

import configparser
import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from modules import channel_occupancy as occupancy_module
from modules.channel_occupancy import (
    INVALID_PACKET_HASH,
    LIVE_METADATA_KEY,
    UNATTRIBUTED_SENDER,
    ChannelOccupancy,
    emitter_label,
    entry_relay_label,
    lora_airtime_ms,
    nearest_rank_percentile,
    normalize_bw_khz,
    normalize_cr_den,
    normalize_hops,
    normalize_packet_hash,
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
# Network view: senders, repeats, hop depth
# ---------------------------------------------------------------------------


class TestNetworkView:
    """What the window is allowed to say about who and what was on air.

    Same rule as the airtime sums: an unknown stays unknown. A talker that was
    heard is counted, a packet with no hash carries no identity, and a repetition
    factor with no evidence is missing, not 1.0.
    """

    def test_repeats_are_counted_from_packet_hashes(self, occupancy):
        """The flood router repeats packets; heard over unique hashes is the repeat factor."""
        for sender, packet_hash in (("alice", "aabbccddeeff0011"), ("alice", "aabbccddeeff0011"), ("bob", "1122334455667788")):
            occupancy.record(30, kind="rx", sender=sender, packet_hash=packet_hash)
        totals = occupancy.window_airtime(60.0)
        assert totals["rx_hash_known"] == 3
        assert totals["rx_unique_packets"] == 2
        assert occupancy.windows()["window"]["dup_factor"] == 1.5

    def test_a_window_with_no_hash_knows_nothing_about_repeats(self, occupancy):
        """x1.0 with no hashed packet would claim a calm network from absent evidence."""
        occupancy.record(30, kind="rx", sender="alice")
        window = occupancy.windows()["window"]
        assert window["hash_known"] == 0 and window["unique_packets"] == 0
        assert window["dup_factor"] is None
        assert window["avg_hops"] is None

    def test_distinct_senders_are_counted_per_window_not_across_windows(self, occupancy):
        """The same talker in the slow window and out of the fast one is not a second talker."""
        monotonic = {"now": 1000.0}
        with patch.object(occupancy_module.time, "monotonic", side_effect=lambda: monotonic["now"]):
            occupancy.record(30, kind="rx", sender="alice", packet_hash="aabbccddeeff0011")
            monotonic["now"] += 15.0
            occupancy.record(30, kind="rx", sender="bob", packet_hash="1122334455667788")
            windows = occupancy.windows()
        assert windows["fast"]["distinct_senders"] == 1
        assert windows["window"]["distinct_senders"] == 2
        assert windows["fast"]["dup_factor"] == 1.0
        assert windows["window"]["dup_factor"] == 1.0
        assert windows["window"]["unique_packets"] == 2

    def test_the_invalid_hash_carries_no_identity(self, occupancy):
        """The all-zero hash is MeshCore's 'could not hash', not a packet everyone shares.

        ``calculate_packet_hash`` returns it on error; counting it would merge every
        unhashable packet into one "unique" message and report a noisy channel as a
        x2 repeat of a single known one.
        """
        occupancy.record(30, kind="rx", sender="alice", packet_hash=INVALID_PACKET_HASH)
        occupancy.record(30, kind="rx", sender="bob", packet_hash="")
        occupancy.record(30, kind="rx", sender="carol")
        totals = occupancy.window_airtime(60.0)
        assert totals["samples"] == 3
        assert totals["rx_hash_known"] == 0
        assert totals["rx_unique_packets"] == 0
        assert occupancy.windows()["window"]["dup_factor"] is None

    def test_direct_is_counted_and_unknown_path_is_not(self, occupancy):
        """0 hops is an answer (heard direct); a missing path is not zero hops.

        Averaging 0 in for unknown paths would read transit traffic as local traffic.
        """
        occupancy.record(30, kind="rx", sender="alice", hops=0)
        occupancy.record(30, kind="rx", sender="bob", hops=2)
        occupancy.record(30, kind="rx", sender="carol")
        totals = occupancy.window_airtime(60.0)
        assert totals["rx_hops_known"] == 2
        assert totals["rx_hops_sum"] == 2
        assert occupancy.windows()["window"]["avg_hops"] == 1.0

    def test_tx_does_not_join_the_network_view(self, occupancy):
        """The bot's own transmissions are not talkers on the network it hears.

        The tx share already answers the bot's own cost, apart and unmerged; mixing
        its own packets into the talkers or the repeats would double-count it.
        """
        occupancy.record(30, kind="tx", sender="bot", packet_hash="aabbccddeeff0011", hops=3)
        window = occupancy.windows()["window"]
        assert window["samples"] == 1
        assert window["distinct_senders"] == 0
        assert window["hash_known"] == 0
        assert window["hops_known"] == 0

    def test_emitter_addresses_are_counted_as_labels_not_stations(self, occupancy):
        """The wire proved two addresses carried these copies; it did not prove two stations.

        One relay can appear under its 2-byte and its 3-byte address, and a prefix
        names a crowd as readily as it names a node, so this count answers how many
        LABELS the window heard, never how many stations they belong to.
        """
        occupancy.record(30, kind="rx", sender="alice", emitter="6201")
        occupancy.record(30, kind="rx", sender="bob", emitter="62010a")
        occupancy.record(30, kind="rx", sender="carol", emitter="6201")
        totals = occupancy.window_airtime(60.0)
        assert totals["rx_emitter_labels"] == 2
        assert occupancy.windows()["window"]["emitter_labels"] == 2

    def test_copies_that_name_no_emitter_add_no_address(self, occupancy):
        """A heard copy with no path, and the bot's own tx, carry no emitter ADDRESS.

        The tx sample is its own emitter but not traffic this radio hears; the
        unattributed bucket names nobody and counting it as an address would turn
        admitted ignorance into a station.
        """
        occupancy.record(30, kind="rx", sender=UNATTRIBUTED_SENDER, emitter=UNATTRIBUTED_SENDER)
        occupancy.record(30, kind="rx", sender="alice")
        occupancy.record(30, kind="tx", sender="bot")
        window = occupancy.windows()["window"]
        assert window["emitter_labels"] == 0
        assert window["unattributed_packets"] == 1

    def test_network_counts_do_not_reach_the_buckets(self, occupancy, conn):
        """Cardinalities do not re-aggregate; a reader summing buckets would over-count.

        The same talker in two buckets is one talker, so these counts stay in the
        ring buffer and the live snapshot, never in the sample rows.
        """
        occupancy.record(30, kind="rx", sender="alice", packet_hash="aabbccddeeff0011", hops=1, at=1_700_000_000.0)
        occupancy.flush(conn)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(occupancy_samples)")}
        assert not columns & {"senders", "unique_packets", "dup_factor", "avg_hops"}
        # The counts reach the snapshot, under exactly the network keys and nowhere else.
        network_keys = {"distinct_senders", "hash_known", "unique_packets", "dup_factor", "hops_known", "avg_hops"}
        window_fields = {key.split("_", 1)[1] for key in occupancy.fields() if key.startswith("window_")}
        assert network_keys <= window_fields


# ---------------------------------------------------------------------------
# Copy cost: what the flood's repetitions take off the channel
# ---------------------------------------------------------------------------


class TestCopyCost:
    """The airtime of the copies after the first one this station heard.

    `dup_factor` counts copies; the copy cost prices them. The rules these
    checks protect: the first copy of an occurrence is free, every later copy
    pays its own modulation, identity-less traffic sits outside the measure
    (unknown, never 0%), and a window the ring buffer cannot cover is a bound,
    not a census.
    """

    def test_first_copy_is_free_and_the_second_pays_its_own_airtime(self, occupancy):
        first = occupancy.record(30, kind="rx", sender="alice", packet_hash="aabbccddeeff0011")
        second = occupancy.record(80, kind="rx", sender="bob", packet_hash="aabbccddeeff0011")
        totals = occupancy.window_airtime(60.0)
        assert totals["rx_copy_ms"] == pytest.approx(second)
        assert totals["rx_hashed_ms"] == pytest.approx(first + second)
        window = occupancy.windows()["window"]
        assert window["rx_copy_ms"] == pytest.approx(round(second, 2))
        assert window["copy_percent"] == pytest.approx(round(second / (first + second) * 100.0, 2))

    def test_tx_copies_pay_no_copy_cost(self, occupancy):
        """The bot's own retransmissions cost airtime but not a heard-copy charge."""
        occupancy.record(30, kind="tx", sender="bot", packet_hash="aabbccddeeff0011")
        occupancy.record(30, kind="tx", sender="bot", packet_hash="aabbccddeeff0011")
        totals = occupancy.window_airtime(60.0)
        assert totals["rx_copy_ms"] == 0.0
        assert totals["rx_hashed_ms"] == 0.0

    def test_identity_less_traffic_sits_outside_the_measure(self, occupancy):
        """A copy of an unhashable packet cannot be matched to itself.

        The share is unknown, not 0%: a rendered zero would claim a flood-free
        channel from the absence of evidence.
        """
        occupancy.record(30, kind="rx", sender="alice")
        occupancy.record(30, kind="rx", sender="bob")
        window = occupancy.windows()["window"]
        assert window["rx_hashed_ms"] == 0.0
        assert window["copy_percent"] is None

    def test_the_horizon_makes_a_re_flood_a_new_first_occurrence(self, occupancy):
        """Two occurrences of one identity a horizon apart pay once each.

        The baseline is this station's hearing, not the network's first
        emission; charging every copy since boot would make a once-a-minute
        beacon pay a copy forever, and the error is a bounded, per-identity
        offset rather than an invented number.
        """
        occupancy.copy_horizon_seconds = 10.0
        monotonic = {"now": 10_000.0}
        occupancy._started_at = monotonic["now"]
        with patch.object(occupancy_module.time, "monotonic", side_effect=lambda: monotonic["now"]):
            occupancy.record(30, kind="rx", packet_hash="aabbccddeeff0011")
            monotonic["now"] += 5.0
            occupancy.record(30, kind="rx", packet_hash="aabbccddeeff0011")
            monotonic["now"] += 20.0
            occupancy.record(30, kind="rx", packet_hash="aabbccddeeff0011")
            totals = occupancy.window_airtime(3600.0)
        assert totals["rx_hashed_ms"] == pytest.approx(totals["rx_copy_ms"] * 3.0)

    def test_the_copy_memory_stays_bounded(self, occupancy):
        """A busy mesh sees thousands of identities; the memory keeps the horizon, not all of them."""
        with patch.object(occupancy_module, "COPY_MEMORY_CAPACITY", 2):
            for index in range(10):
                occupancy.record(30, kind="rx", packet_hash=f"{index:016x}")
        assert len(occupancy._copy_seen) <= 2

    def test_the_truncated_flag_tracks_what_the_buffer_can_prove(self, occupancy):
        """A window the ring buffer cannot cover is bounded, not census.

        Two limits, honest at each one: a process younger than the window never
        heard what came before it, and an overflowed deque has dropped copies
        it can no longer count.
        """
        occupancy.record(30, kind="rx", sender="alice", packet_hash="aabbccddeeff0011")
        occupancy.record(30, kind="rx", sender="bob", packet_hash="aabbccddeeff0011")
        assert occupancy.windows()["window"]["truncated"] is True
        occupancy._started_at = time.monotonic() - 3600.0
        assert occupancy.windows()["window"]["truncated"] is False
        occupancy.max_samples = 2
        occupancy.record(30, kind="rx", sender="carol", packet_hash="aabbccddeeff0011")
        assert occupancy.windows()["window"]["truncated"] is True

    def test_the_previous_window_compares_equal_lengths(self, occupancy):
        """The window one length back, plus a stated change over it."""
        monotonic = {"now": 10_000.0}
        occupancy._started_at = 9_000.0
        with patch.object(occupancy_module.time, "monotonic", side_effect=lambda: monotonic["now"]):
            monotonic["now"] = 9_930.0
            occupancy.record(30, kind="rx", packet_hash="1122334455667788")
            monotonic["now"] = 9_995.0
            occupancy.record(90, kind="rx", packet_hash="aabbccddeeff0011")
            window = occupancy.windows()["window"]
        assert window["previous"] is not None
        assert window["previous"]["samples"] == 1
        assert window["trend"]["status"] == "comparable"
        assert window["trend"]["rx_delta_ms"] == pytest.approx(round(window["rx_ms"] - window["previous"]["rx_ms"], 2))

    def test_an_empty_previous_window_is_absent_evidence_not_a_quiet_channel(self, occupancy):
        """A fresh start with samples now must not read as a sudden flood.

        The previous window either cannot be covered or holds no sample, and
        both answer `insufficient_history`: zero samples is absent evidence,
        not a quiet baseline.
        """
        occupancy.record(30, kind="rx", sender="alice", packet_hash="aabbccddeeff0011")
        window = occupancy.windows()["window"]
        assert window["previous"] is None
        assert window["trend"]["status"] == "insufficient_history"
        occupancy._started_at = time.monotonic() - 3600.0
        window = occupancy.windows()["window"]
        assert window["previous"]["samples"] == 0
        assert window["trend"]["status"] == "insufficient_history"

    def test_the_copy_fields_reach_the_live_snapshot(self, occupancy):
        occupancy.record(30, kind="rx", sender="alice", packet_hash="aabbccddeeff0011")
        fields = occupancy.fields()
        assert "window_copy_percent" in fields
        assert "window_rx_copy_ms" in fields
        assert "window_truncated" in fields
        assert "window_prev_rx_ms" in fields

    def test_the_bucket_stores_the_hashed_and_copy_sums(self, occupancy, conn):
        """Copy cost is a sum, so it belongs in the buckets the cardinalities cannot enter."""
        first = occupancy.record(30, kind="rx", sender="alice", packet_hash="aabbccddeeff0011", at=1_700_000_000.0)
        second = occupancy.record(80, kind="rx", sender="bob", packet_hash="aabbccddeeff0011", at=1_700_000_001.0)
        occupancy.flush(conn)
        stored = conn.execute(
            "SELECT rx_hashed_ms, rx_copy_ms FROM occupancy_samples"
        ).fetchone()
        assert stored["rx_hashed_ms"] == pytest.approx(first + second)
        assert stored["rx_copy_ms"] == pytest.approx(second)

    def test_history_states_the_copy_share_when_every_bucket_knows_it(self, occupancy, conn):
        occupancy.record(30, kind="rx", packet_hash="aabbccddeeff0011", at=1_700_000_000.0)
        occupancy.record(30, kind="rx", packet_hash="aabbccddeeff0011", at=1_700_000_030.0)
        occupancy.flush(conn)
        history = occupancy.history(conn, 1_699_999_900.0, 1_700_000_120.0)
        assert history["total_rx_hashed_ms"] is not None
        assert history["total_rx_copy_ms"] is not None
        assert history["copy_percent"] is not None

    def test_history_keeps_the_copy_share_unknown_across_the_split(self, occupancy, conn):
        """A bucket written before migration 40 carries NULL, and NULL is not zero copies."""
        occupancy.record(30, kind="rx", packet_hash="aabbccddeeff0011", at=1_700_000_000.0)
        occupancy.record(30, kind="rx", packet_hash="aabbccddeeff0011", at=1_700_000_030.0)
        occupancy.flush(conn)
        conn.execute(
            "INSERT INTO occupancy_samples (bucket_ts, sample_seconds, rx_ms, tx_ms, packets, rx_bytes) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (1_699_999_940, 60, 1000.0, 0.0, 1, 30),
        )
        conn.commit()
        history = occupancy.history(conn, 1_699_999_900.0, 1_700_000_120.0)
        assert history["total_rx_hashed_ms"] is None
        assert history["copy_percent"] is None
        known = [b for b in history["buckets"] if b["rx_hashed_ms"] is not None]
        assert known, "the per-bucket values ride along so the reader can split known from unknown"

    def test_migration_40_adds_the_copy_columns_as_nullable(self, conn):
        columns = {
            row[1]: row[2]
            for row in conn.execute("PRAGMA table_info(occupancy_samples)")
        }
        assert columns["rx_hashed_ms"] == "REAL"
        assert columns["rx_copy_ms"] == "REAL"


# ---------------------------------------------------------------------------
# Unattributed heard airtime: a bucket, not a station
# ---------------------------------------------------------------------------


class TestUnattributedSender:
    """The reserved bucket keeps the senders table complete without inventing a talker.

    Every heard copy costs the channel, even one no identity covers; the bucket owns
    that airtime in the table, and the talker count stays what this radio can name.
    """

    def test_unattributed_airtime_is_counted_but_not_as_a_talker(self, occupancy):
        occupancy.record(30, kind="rx", sender=UNATTRIBUTED_SENDER, packet_hash="aabbccddeeff0011")
        occupancy.record(40, kind="rx", sender="alice", packet_hash="1122334455667788")
        window = occupancy.windows()["window"]
        assert window["distinct_senders"] == 1
        assert window["unattributed_packets"] == 1
        assert window["rx_ms"] > 0.0

    def test_unattributed_reaches_the_senders_table(self, occupancy, conn):
        """The table has to add up to the heard channel: dropped copies would hide traffic."""
        occupancy.record(30, kind="rx", sender=UNATTRIBUTED_SENDER, at=1_700_000_000.0)
        occupancy.flush(conn)
        senders = {row["sender"] for row in conn.execute("SELECT sender FROM occupancy_senders")}
        assert senders == {UNATTRIBUTED_SENDER}


# ---------------------------------------------------------------------------
# Hash → author attribution
# ---------------------------------------------------------------------------


AUTHOR = "a" * 64


def _day_rows(conn, sender):
    return list(conn.execute("SELECT * FROM occupancy_sender_days WHERE sender = ?", (sender,)))


class TestAuthorAttribution:
    """A decrypted message naming its author lifts that packet's copies out of the unknown.

    The note is the radio's own authenticated decryption — not a guess — and it
    only reaches copies still pending in memory: a flushed day row is history.
    """

    def test_known_hash_attributes_from_the_start(self, occupancy, conn):
        occupancy.note_author("aabbccddeeff0011", AUTHOR)
        occupancy.record(30, kind="rx", sender=UNATTRIBUTED_SENDER, packet_hash="aabbccddeeff0011")
        occupancy.flush(conn)
        assert len(_day_rows(conn, AUTHOR)) == 1
        assert not _day_rows(conn, UNATTRIBUTED_SENDER)

    def test_pending_unattributed_moves_to_the_author_before_the_flush(self, occupancy, conn):
        occupancy.record(30, kind="rx", sender=UNATTRIBUTED_SENDER, packet_hash="aabbccddeeff0011")
        occupancy.record(30, kind="rx", sender=UNATTRIBUTED_SENDER, packet_hash="9988776655443322")
        assert occupancy.note_author("aabbccddeeff0011", AUTHOR) is True
        occupancy.flush(conn)
        attributed = _day_rows(conn, AUTHOR)
        assert len(attributed) == 1
        assert attributed[0]["packets"] == 1
        # The other copy is tracked and still inside its attribution grace: it
        # is held in memory rather than written, and a note arriving this late
        # still finds it.
        assert not _day_rows(conn, UNATTRIBUTED_SENDER)
        assert occupancy.note_author("9988776655443322", AUTHOR) is True
        occupancy.flush(conn)
        attributed = _day_rows(conn, AUTHOR)
        assert len(attributed) == 1
        assert attributed[0]["packets"] == 2
        assert not _day_rows(conn, UNATTRIBUTED_SENDER)

    def test_a_flushed_copy_is_history(self, occupancy, conn):
        """Attribution never rewrites a row that was already written."""
        occupancy.record(30, kind="rx", sender=UNATTRIBUTED_SENDER, packet_hash="aabbccddeeff0011")
        occupancy.flush(conn, force=True)
        occupancy.note_author("aabbccddeeff0011", AUTHOR)
        occupancy.flush(conn)
        assert not _day_rows(conn, AUTHOR)
        unattributed = _day_rows(conn, UNATTRIBUTED_SENDER)
        assert len(unattributed) == 1
        assert unattributed[0]["packets"] == 1

    def test_the_grace_holds_a_tracked_copy_until_its_window_closes(self, occupancy, conn):
        """The writer drains faster than the radio decrypts, so the flush waits."""
        occupancy.record(30, kind="rx", sender=UNATTRIBUTED_SENDER, packet_hash="aabbccddeeff0011")
        occupancy.flush(conn)
        assert not _day_rows(conn, UNATTRIBUTED_SENDER)
        assert occupancy.note_author("aabbccddeeff0011", AUTHOR) is True
        occupancy.flush(conn)
        attributed = _day_rows(conn, AUTHOR)
        assert len(attributed) == 1
        assert attributed[0]["packets"] == 1
        assert not _day_rows(conn, UNATTRIBUTED_SENDER)

    def test_a_closed_window_is_written_as_it_was_heard(self, occupancy, conn):
        """Past the grace the increment is written unknown, and a later note is too late."""
        occupancy.record(30, kind="rx", sender=UNATTRIBUTED_SENDER, packet_hash="aabbccddeeff0011")
        occupancy.attribution_grace_sec = 0.0
        occupancy.flush(conn)
        occupancy.note_author("aabbccddeeff0011", AUTHOR)
        occupancy.flush(conn)
        assert not _day_rows(conn, AUTHOR)
        unattributed = _day_rows(conn, UNATTRIBUTED_SENDER)
        assert len(unattributed) == 1
        assert unattributed[0]["packets"] == 1

    def test_an_untracked_unknown_is_written_at_once(self, occupancy, conn):
        """Without a packet identity there is nothing to wait for: the grace is moot."""
        occupancy.record(30, kind="rx", sender=UNATTRIBUTED_SENDER)
        occupancy.flush(conn)
        unattributed = _day_rows(conn, UNATTRIBUTED_SENDER)
        assert len(unattributed) == 1
        assert unattributed[0]["packets"] == 1

    def test_a_forced_flush_writes_everything_the_grace_holds(self, occupancy, conn):
        """Shutdown is the last write this process will make: nothing stays behind."""
        occupancy.record(30, kind="rx", sender=UNATTRIBUTED_SENDER, packet_hash="aabbccddeeff0011")
        occupancy.flush(conn)
        assert not _day_rows(conn, UNATTRIBUTED_SENDER)
        occupancy.flush(conn, force=True)
        unattributed = _day_rows(conn, UNATTRIBUTED_SENDER)
        assert len(unattributed) == 1
        assert unattributed[0]["packets"] == 1

    def test_a_day_can_hold_and_write_at_the_same_time(self, occupancy, conn):
        """The hold is per packet identity, not per flush: untracked copies go through."""
        occupancy.record(30, kind="rx", sender=UNATTRIBUTED_SENDER)
        occupancy.record(40, kind="rx", sender=UNATTRIBUTED_SENDER, packet_hash="aabbccddeeff0011")
        occupancy.flush(conn)
        unattributed = _day_rows(conn, UNATTRIBUTED_SENDER)
        assert len(unattributed) == 1
        assert unattributed[0]["packets"] == 1

    def test_nothing_is_ever_double_counted(self, occupancy, conn):
        occupancy.record(30, kind="rx", sender=UNATTRIBUTED_SENDER, packet_hash="aabbccddeeff0011")
        occupancy.record(30, kind="rx", sender=UNATTRIBUTED_SENDER, packet_hash="aabbccddeeff0011")
        occupancy.note_author("aabbccddeeff0011", AUTHOR)
        occupancy.flush(conn)
        attributed = _day_rows(conn, AUTHOR)
        assert len(attributed) == 1
        assert attributed[0]["packets"] == 2
        left = _day_rows(conn, UNATTRIBUTED_SENDER)
        assert not left or left[0]["packets"] == 0

    def test_a_cache_attributed_copy_is_never_moved_twice(self, occupancy, conn):
        """The cache names the copy from the start, so the unknown bucket never holds it."""
        occupancy.note_author("aabbccddeeff0011", AUTHOR)
        occupancy.record(30, kind="rx", sender=UNATTRIBUTED_SENDER, packet_hash="aabbccddeeff0011")
        occupancy.note_author("aabbccddeeff0011", AUTHOR)  # flood copies re-note the same hash
        occupancy.flush(conn)
        attributed = _day_rows(conn, AUTHOR)
        assert len(attributed) == 1
        assert attributed[0]["packets"] == 1
        left = _day_rows(conn, UNATTRIBUTED_SENDER)
        assert not left or left[0]["packets"] == 0

    def test_an_unusable_note_is_refused(self, occupancy):
        assert occupancy.note_author("aabbccddeeff0011", "aabb") is False  # a prefix is not an identity
        assert occupancy.note_author("aabbccddeeff0011", "z" * 64) is False
        assert occupancy.note_author(None, AUTHOR) is False
        assert occupancy.note_author("0000000000000000", AUTHOR) is False
        occupancy.record(30, kind="rx", sender=UNATTRIBUTED_SENDER, packet_hash="aabbccddeeff0011")
        window = occupancy.windows()["window"]
        assert window["unattributed_packets"] == 1

    def test_the_emitter_ledger_is_untouched(self, occupancy, conn):
        """An author wrote the payload; the emitter carried the copy. Two facts, never merged."""
        occupancy.record(
            30,
            kind="rx",
            sender=UNATTRIBUTED_SENDER,
            packet_hash="aabbccddeeff0011",
            emitter="62010a",
        )
        occupancy.note_author("aabbccddeeff0011", AUTHOR)
        occupancy.flush(conn)
        emitters = list(conn.execute("SELECT * FROM occupancy_emitter_days"))
        assert len(emitters) == 1
        assert emitters[0]["emitter"] == "62010a"
        assert _day_rows(conn, AUTHOR)[0]["packets"] == 1

    def test_the_cache_is_bounded(self, occupancy):
        for i in range(6000):
            occupancy.note_author(f"cafe00000000{i:04x}", AUTHOR)
        assert len(occupancy._authors) <= 4096
        # The oldest notes leave first; the newest still attribute.
        occupancy.record(30, kind="rx", sender=UNATTRIBUTED_SENDER, packet_hash="cafe00000000176f")
        window = occupancy.windows()["window"]
        assert window["unattributed_packets"] == 0

    def test_advert_slice_travels_with_the_copy(self, occupancy, conn):
        occupancy.record(
            30, kind="rx", sender=UNATTRIBUTED_SENDER, packet_hash="aabbccddeeff0011", advert=True
        )
        occupancy.note_author("aabbccddeeff0011", AUTHOR)
        occupancy.flush(conn)
        row = _day_rows(conn, AUTHOR)[0]
        assert row["advert_ms"] > 0
        left = _day_rows(conn, UNATTRIBUTED_SENDER)
        assert not left or left[0]["advert_ms"] == 0


# ---------------------------------------------------------------------------
# The advert slice of the sender share
# ---------------------------------------------------------------------------


class TestAdvertSlice:
    """A station's presence cost kept apart from its talking cost, inside its own airtime.

    An ADVERT is the maintenance traffic every MeshCore node floods to exist; a
    message is traffic it chose to send.  Summing the two into one sender's airtime
    reads as if a busy repeater were a chatty user, so the senders table carries the
    advert slice apart — and keeps it NULL, not 0, for a sender whose traffic
    predates the split, because an unknown share rendered as zero is a made-up fact.
    """

    def test_advert_slices_reach_the_senders_table(self, occupancy, conn):
        ad_ms = occupancy.record(125, kind="rx", sender="alice", advert=True, at=1_700_000_000.0)
        occupancy.record(30, kind="rx", sender="alice", advert=False, at=1_700_000_001.0)
        occupancy.flush(conn)
        rows = {row["sender"]: row for row in occupancy.top_senders(conn)}
        assert rows["alice"]["advert_ms"] == pytest.approx(ad_ms)
        assert rows["alice"]["airtime_ms"] > ad_ms

    def test_a_station_that_only_advertises_is_all_advert(self, occupancy, conn):
        ad_ms = occupancy.record(125, kind="rx", sender="bob", advert=True, at=1_700_000_000.0)
        occupancy.flush(conn)
        row = occupancy.top_senders(conn)[0]
        assert row["advert_ms"] == pytest.approx(row["airtime_ms"])
        assert row["advert_ms"] == pytest.approx(ad_ms)

    def test_the_slice_accumulates_across_flushes(self, occupancy, conn):
        first = occupancy.record(125, kind="rx", sender="alice", advert=True, at=1_700_000_000.0)
        occupancy.flush(conn)
        occupancy.record(30, kind="rx", sender="alice", advert=False, at=1_700_000_030.0)
        second = occupancy.record(125, kind="rx", sender="alice", advert=True, at=1_700_000_031.0)
        occupancy.flush(conn)
        row = occupancy.top_senders(conn)[0]
        assert row["advert_ms"] == pytest.approx(first + second)

    def test_a_sender_from_before_the_split_keeps_an_unknown_share(self, occupancy, conn):
        """NULL is the honest answer: the split was never recorded for that traffic."""
        conn.execute(
            "INSERT INTO occupancy_senders (sender, airtime_ms, packets, first_seen, last_seen) "
            "VALUES ('old', 100.0, 3, 1.0, 2.0)"
        )
        conn.commit()
        assert occupancy.top_senders(conn)[0]["advert_ms"] is None

    def test_a_legacy_row_resumes_the_slice_from_its_next_write(self, occupancy, conn):
        """A legacy row starts counting the slice again rather than staying NULL — the
        share it then states is a floor over its retained history, whose adverts were
        never split."""
        conn.execute(
            "INSERT INTO occupancy_senders (sender, airtime_ms, packets, first_seen, last_seen) "
            "VALUES ('old', 100.0, 3, 1.0, 2.0)"
        )
        conn.commit()
        written = occupancy.record(125, kind="rx", sender="old", advert=True, at=1_700_000_000.0)
        occupancy.flush(conn)
        assert occupancy.top_senders(conn)[0]["advert_ms"] == pytest.approx(written)


# ---------------------------------------------------------------------------
# Emitter ledger: who physically put each heard copy on the air
# ---------------------------------------------------------------------------


class TestEmitterLabel:
    def test_the_last_path_entry_is_the_emitter(self):
        """MeshCore flood routing names the transmitter on the wire: the path's last
        node address is the relay whose antenna carried this copy."""
        assert emitter_label(["B06D1C", "AAE91A", "D4819F", "62010A"], "writer", 4) == "62010a"

    def test_a_two_byte_node_address_is_kept_as_a_prefix(self):
        """The wire carries 2 or 3 leading bytes of the relay key; the label keeps the
        width it arrived with — resolution to a name is the reader's, not this one's."""
        assert emitter_label(["1D90", "6938", "6201"], "writer", 3) == "6201"

    def test_a_direct_copy_is_the_senders_own_antenna(self):
        full = "12365aa9" + "0" * 56
        assert emitter_label(None, full, 0) == full
        assert emitter_label([], full, 0) == full

    def test_a_short_sender_names_nobody_for_a_direct_copy(self):
        """Without a path the only candidate emitter is the sender, and a truncated
        sender key is not an identity: an admitted unknown beats a probable name."""
        assert emitter_label([], "AD3206", 0) == UNATTRIBUTED_SENDER

    def test_a_trace_path_hash_is_not_an_identity(self):
        """TRACE paths hold per-hop SNR hashes, not node addresses; an 8-byte hash
        must not be read as a station just because it is hex."""
        assert emitter_label(["A1B2C3D4E5F60708", "090A0B0C0D0E0F10"], None, 2) == UNATTRIBUTED_SENDER


class TestEmitterLedger:
    def test_a_relayed_copy_is_charged_to_the_relay_not_the_writer(self, occupancy, conn):
        """The emitter answers who transmitted; the senders table answers who wrote.

        A repeater beside the antenna can own this channel without ever appearing in
        the senders' table, because relaying never puts its own payload on the air.
        """
        occupancy.record(
            40, kind="rx", sender="12365aa9" + "0" * 56,
            emitter=emitter_label(["B06D1C", "62010A"], None, 2),
        )
        occupancy.flush(conn)
        emitters = {row["emitter"]: row for row in occupancy.top_emitters(conn)}
        assert set(emitters) == {"62010a"}
        assert emitters["62010a"]["packets"] == 1

    def test_the_two_ledgers_hold_the_same_copy_once_per_fact_and_never_merge(self, occupancy, conn):
        """One copy, two subjects: carrier and emitter.  The tables are separate on
        purpose, and a sum across them would bill the channel twice."""
        full = "12365aa9" + "0" * 56
        occupancy.record(40, kind="rx", sender=full, emitter="62010a")
        occupancy.flush(conn)
        sender_row = conn.execute("SELECT airtime_ms FROM occupancy_senders WHERE sender = ?", (full,)).fetchone()
        emitter_row = conn.execute("SELECT airtime_ms FROM occupancy_emitters WHERE emitter = '62010a'").fetchone()
        assert sender_row["airtime_ms"] == emitter_row["airtime_ms"]

    def test_a_transmission_is_its_own_emitter(self, occupancy, conn):
        """The bot asked the radio to send: whatever the sender label is, that same
        station physically put the copy on the air."""
        own_key = "aabbccddeeff0011223344556677889900aabbccddeeff001122334455667788aa"
        occupancy.record(40, kind="tx", sender=own_key)
        occupancy.flush(conn)
        emitters = occupancy.top_emitters(conn)
        assert len(emitters) == 1
        assert emitters[0]["emitter"] == own_key

    def test_the_advert_slice_splits_per_emitter(self, occupancy, conn):
        """An advert repeated by a relay spends the RELAY's presence budget too: the
        split is kept per emitter exactly as it is per sender."""
        occupancy.record(125, kind="rx", sender="writer", emitter="62010a", advert=True)
        occupancy.record(30, kind="rx", sender="writer", emitter="62010a", advert=False)
        occupancy.flush(conn)
        row = occupancy.top_emitters(conn)[0]
        assert row["advert_ms"] > 0
        assert row["advert_ms"] < row["airtime_ms"]

    def test_emitter_increments_accumulate_across_flushes(self, occupancy, conn):
        base = int(occupancy_module.time.time()) // 60 * 60
        first = occupancy.record(40, kind="rx", sender="writer", emitter="62010a", at=base)
        occupancy.flush(conn)
        occupancy.record(30, kind="rx", sender="writer", emitter="62010a", at=base + 10)
        occupancy.flush(conn)
        row = occupancy.top_emitters(conn)[0]
        assert row["packets"] == 2
        assert row["airtime_ms"] > first

    def test_top_emitters_ranks_by_airtime_and_caps_the_limit(self, occupancy, conn):
        for index, size in enumerate((20, 60, 40)):
            occupancy.record(size, kind="rx", sender="writer", emitter=f"62010{index}")
        occupancy.flush(conn)
        rows = occupancy.top_emitters(conn, limit=2)
        assert len(rows) == 2
        assert rows[0]["emitter"] == "620101"


# ---------------------------------------------------------------------------
# Entry-relay ledger: where each relayed copy entered the mesh
# ---------------------------------------------------------------------------


class TestEntryRelayLabel:
    def test_the_first_path_entry_is_the_entry_relay(self):
        """The flood path's FIRST node address is the first relay that repeated the
        packet — the gateway through which the copy entered this mesh."""
        assert entry_relay_label(["B06D1C", "AAE91A", "D4819F", "62010A"]) == "b06d1c"

    def test_a_direct_copy_entered_nothing(self):
        """No path means no relay; charging a direct copy to a relay would invent a
        hop the wire did not carry."""
        assert entry_relay_label(None) is None
        assert entry_relay_label([]) is None

    def test_a_two_byte_node_address_is_kept_as_a_prefix(self):
        assert entry_relay_label(["1D90", "6938"]) == "1d90"

    def test_a_trace_path_hash_is_not_an_address(self):
        """TRACE paths hold per-hop SNR hashes, not node addresses."""
        assert entry_relay_label(["A1B2C3D4E5F60708", "090A0B0C0D0E0F10"]) is None

    def test_the_entry_relay_is_not_the_emitter(self):
        """Same path, read from the other end: first hop is the gateway, last hop is
        the antenna. A one-hop path is the only case where they are the same node."""
        path = ["B06D1C", "62010A"]
        assert entry_relay_label(path) != emitter_label(path, None, 2)
        assert entry_relay_label(["62010A"]) == emitter_label(["62010A"], None, 1)


class TestEntryRelayLedger:
    def test_a_relayed_copy_is_charged_to_its_first_hop(self, occupancy, conn):
        """The entry relay answers where the copy entered; neither senders nor
        emitters answer that, and this table is not the emitter's reversed."""
        occupancy.record(
            40, kind="rx", sender="12365aa9" + "0" * 56,
            emitter=emitter_label(["B06D1C", "62010A"], None, 2),
            entry_relay=entry_relay_label(["B06D1C", "62010A"]),
        )
        occupancy.flush(conn)
        relays = {row["relay"]: row for row in occupancy.top_relays(conn)}
        assert set(relays) == {"b06d1c"}
        assert relays["b06d1c"]["packets"] == 1

    def test_a_direct_copy_has_no_relay_row(self, occupancy, conn):
        """Heard direct is fully accounted by the emitter ledger; the relay table
        stays deliberately smaller, and absence here is not silence."""
        occupancy.record(40, kind="rx", sender="12365aa9" + "0" * 56, emitter="62010a")
        occupancy.flush(conn)
        assert occupancy.top_relays(conn) == []

    def test_only_a_node_address_names_a_relay(self, occupancy, conn):
        """A full key never arrives on a flood path, and non-hex names nothing; a
        caller that hands over either gets no row rather than an invented station."""
        occupancy.record(40, kind="rx", sender="w", entry_relay="12365aa9" + "0" * 56)
        occupancy.record(40, kind="rx", sender="w", entry_relay="ZZZZ")
        occupancy.record(40, kind="rx", sender="w", entry_relay="6201")
        occupancy.flush(conn)
        assert [row["relay"] for row in occupancy.top_relays(conn)] == ["6201"]

    def test_the_three_ledgers_hold_one_copy_once_per_fact(self, occupancy, conn):
        """Writer, gateway and antenna are three facts about one copy. The airtime
        appears once in each table, and no sum of tables describes the channel."""
        full = "12365aa9" + "0" * 56
        occupancy.record(40, kind="rx", sender=full, emitter="62010a", entry_relay="b06d1c")
        occupancy.flush(conn)
        airtime = {
            table: conn.execute(f"SELECT airtime_ms FROM {table}").fetchone()[0]
            for table in ("occupancy_senders", "occupancy_emitters", "occupancy_relays")
        }
        assert airtime["occupancy_senders"] == airtime["occupancy_emitters"] == airtime["occupancy_relays"]
        assert len(occupancy.top_relays(conn)) == 1

    def test_the_advert_slice_splits_per_relay(self, occupancy, conn):
        """A repeated advert spends the gateway's presence budget too, kept apart
        exactly as it is per sender and per emitter."""
        occupancy.record(125, kind="rx", sender="writer", entry_relay="b06d1c", advert=True)
        occupancy.record(30, kind="rx", sender="writer", entry_relay="b06d1c", advert=False)
        occupancy.flush(conn)
        row = occupancy.top_relays(conn)[0]
        assert row["advert_ms"] > 0
        assert row["advert_ms"] < row["airtime_ms"]

    def test_relay_increments_accumulate_across_flushes(self, occupancy, conn):
        base = int(occupancy_module.time.time()) // 60 * 60
        first = occupancy.record(40, kind="rx", sender="writer", entry_relay="b06d1c", at=base)
        occupancy.flush(conn)
        occupancy.record(30, kind="rx", sender="writer", entry_relay="b06d1c", at=base + 10)
        occupancy.flush(conn)
        row = occupancy.top_relays(conn)[0]
        assert row["packets"] == 2
        assert row["airtime_ms"] > first

    def test_top_relays_ranks_by_airtime_and_caps_the_limit(self, occupancy, conn):
        for index, size in enumerate((20, 60, 40)):
            occupancy.record(size, kind="rx", sender="writer", entry_relay=f"b06d1{index}")
        occupancy.flush(conn)
        rows = occupancy.top_relays(conn, limit=2)
        assert len(rows) == 2
        assert rows[0]["relay"] == "b06d11"

    def test_the_relay_ledger_never_follows_an_author_note(self, occupancy, conn):
        """A late decryption names the WRITER; where the copy entered the mesh was
        already proved on the wire and does not move with an author."""
        occupancy.record(
            30, kind="rx", sender=UNATTRIBUTED_SENDER,
            packet_hash="aabbccddeeff0011", entry_relay="b06d1c",
        )
        occupancy.note_author("aabbccddeeff0011", AUTHOR)
        occupancy.flush(conn)
        relays = list(conn.execute("SELECT * FROM occupancy_relay_days"))
        assert len(relays) == 1
        assert relays[0]["relay"] == "b06d1c"
        assert _day_rows(conn, AUTHOR)[0]["packets"] == 1


# ---------------------------------------------------------------------------
# Period windows: the grain the history selector reads at
# ---------------------------------------------------------------------------


class TestPeriodWindows:
    """The cumulative ledgers cannot be cut back to a period, so the day rows carry it."""

    def test_samples_on_two_utc_days_get_two_day_rows(self, occupancy, conn):
        """Day attribution follows the sample's clock, not the flush's.

        A batch that straddles a UTC midnight must not blur two days into one row:
        every window is a sum over days, so a mixed row makes every window that
        contains one of those two days wrong.
        """
        day = int(occupancy_module.time.time()) // 86400 * 86400
        occupancy.record(40, kind="rx", sender="w", at=day - 5)
        occupancy.record(30, kind="rx", sender="w", at=day + 5)
        occupancy.flush(conn)
        rows = conn.execute(
            "SELECT day_ts, packets FROM occupancy_sender_days WHERE sender = 'w' ORDER BY day_ts"
        ).fetchall()
        assert [row["day_ts"] for row in rows] == [day - 86400, day]
        assert sum(row["packets"] for row in rows) == 2

    def test_the_cumulative_rows_are_aggregated_from_the_day_rows(self, occupancy, conn):
        """One fact, two grains: each station's total is the sum of its day rows.

        Two write paths fed from the same increments can drift; building the totals
        from the day rows the flush just wrote leaves only arithmetic able to
        disagree, and the reader may trust that one table describes the other.
        """
        day = int(occupancy_module.time.time()) // 86400 * 86400
        occupancy.record(40, kind="rx", sender="w", emitter="62010a", entry_relay="b06d1c", advert=True, at=day - 43200)
        occupancy.record(30, kind="rx", sender="w", emitter="62010a", entry_relay="b06d1c", at=day + 3600)
        occupancy.flush(conn)
        for day_table, ledger in (
            ("occupancy_sender_days", "occupancy_senders"),
            ("occupancy_emitter_days", "occupancy_emitters"),
            ("occupancy_relay_days", "occupancy_relays"),
        ):
            days = conn.execute(
                f"SELECT SUM(airtime_ms) AS a, SUM(packets) AS p, SUM(advert_ms) AS v FROM {day_table}"
            ).fetchone()
            total = conn.execute(
                f"SELECT airtime_ms AS a, packets AS p, advert_ms AS v FROM {ledger}"
            ).fetchone()
            assert total["p"] == days["p"] == 2
            assert total["a"] == pytest.approx(days["a"])
            assert total["v"] == pytest.approx(days["v"])

    def test_the_cutoff_day_keeps_whole_utc_days(self, occupancy):
        """A "last 24h" selector over whole-day rows counts the oldest overlapping day whole.

        The storage grain is the day, so the window says so; pretending the sum
        could stop at yesterday's noon would price part of a day nobody measured.
        """
        day = 1_763_164_800  # a UTC midnight
        now = day + 43_200  # noon
        assert occupancy.window_cutoff_day(1, now) == day - 86400
        assert occupancy.window_cutoff_day(7, now) == day - 7 * 86400
        assert occupancy.window_cutoff_day(0, now) == day - 86400

    def test_window_sums_stop_at_the_cutoff_and_rank_by_airtime(self, occupancy, conn):
        """The window sees only the days inside it, and orders them as the ledgers do."""
        day = int(occupancy_module.time.time()) // 86400 * 86400
        occupancy.record(20, kind="rx", sender="w", emitter="62010a", entry_relay="aa11", at=day - 3 * 86400)
        occupancy.record(60, kind="rx", sender="w", emitter="62010a", entry_relay="bb2222", at=day + 10)
        occupancy.record(40, kind="rx", sender="w", emitter="62011b", entry_relay="cc33", at=day + 20)
        occupancy.flush(conn)
        cutoff = occupancy.window_cutoff_day(1)
        senders = occupancy.top_senders_window(conn, cutoff)
        assert [row["sender"] for row in senders] == ["w"]
        assert senders[0]["packets"] == 2
        lifetime = occupancy.top_senders(conn)[0]
        assert senders[0]["airtime_ms"] < lifetime["airtime_ms"]
        emitters = occupancy.top_emitters_window(conn, cutoff, limit=1)
        assert [row["emitter"] for row in emitters] == ["62010a"]
        # Same day-aligned window for the relay ledger: the gateway of the copy that
        # predates the cutoff is out, and the two inside rank by airtime.
        relays = occupancy.top_relays_window(conn, cutoff)
        assert [row["relay"] for row in relays] == ["bb2222", "cc33"]

    def test_a_window_without_day_rows_is_empty_not_zero(self, occupancy, conn):
        """No day row inside the window means no copy recorded there — the table stays empty.

        Returning a row of zeros would let the page render a quiet station the
        radio never actually heard being quiet: absence of records is not a
        measurement of silence.
        """
        day = int(occupancy_module.time.time()) // 86400 * 86400
        occupancy.record(40, kind="rx", sender="w", emitter="62010a", at=day - 20 * 86400)
        occupancy.flush(conn)
        cutoff = occupancy.window_cutoff_day(1)
        assert occupancy.top_senders_window(conn, cutoff) == []
        assert occupancy.top_emitters_window(conn, cutoff) == []


# ---------------------------------------------------------------------------
# The channel floor: what a flood tree proves beyond this antenna
# ---------------------------------------------------------------------------


class TestChannelFloor:
    """The floor adds the copies a heard tree proves to the airtime this radio heard.

    Carrier sense is local; the channel is one shared medium. A copy heard four
    relays deep was put on air at least four times, whether or not those earlier
    copies ever reached this antenna — and a share above 100% is then a fact about
    parallel traffic, not an overflow. Parallel branches no copy proves stay outside
    the floor: it is a floor, never a ceiling.
    """

    def test_a_heard_direct_copy_is_its_own_floor(self, occupancy):
        heard = occupancy.record(30, kind="rx", sender="alice", packet_hash="aabbccddeeff0011", hops=0)
        assert occupancy.window_airtime(60.0)["rx_floor_ms"] == pytest.approx(heard)

    def test_a_deep_path_prices_the_copies_that_never_reached_this_antenna(self, occupancy):
        mod = occupancy.modulation()
        heard = occupancy.record(40, kind="rx", sender="alice", packet_hash="aabbccddeeff0011", hops=3)
        shallower = sum(lora_airtime_ms(40 - 2 * (3 - depth), **mod) for depth in range(3))
        totals = occupancy.window_airtime(60.0)
        assert totals["rx_floor_ms"] == pytest.approx(heard + shallower)
        assert totals["rx_floor_ms"] > totals["rx_ms"]

    def test_heard_repeats_pay_separately_and_missing_depths_once(self, occupancy):
        """Two heard copies at depth 2 are two transmissions; the tree's depths 0 and 1 are one each."""
        mod = occupancy.modulation()
        first = occupancy.record(34, kind="rx", packet_hash="aabbccddeeff0011", hops=2)
        second = occupancy.record(34, kind="rx", packet_hash="aabbccddeeff0011", hops=2)
        expected = first + second + lora_airtime_ms(30, **mod) + lora_airtime_ms(32, **mod)
        assert occupancy.window_airtime(60.0)["rx_floor_ms"] == pytest.approx(expected)

    def test_missing_copies_are_priced_with_the_packets_own_path_width(self, occupancy):
        """MeshCore path entries are 2 or 3 bytes depending on transport size, so the
        width one relay adds is a fact of the packet, not a constant.  A packet
        recorded with its measured 3-byte width must not be re-priced with the
        fallback: the shallower copies would come out too long."""
        mod = occupancy.modulation()
        occupancy.record(40, kind="rx", packet_hash="aabbccddeeff0011", hops=2, path_bytes_per_hop=3)
        expected = sum(lora_airtime_ms(size, **mod) for size in (34, 37, 40))
        assert occupancy.window_airtime(60.0)["rx_floor_ms"] == pytest.approx(expected)

    def test_a_copy_without_hash_proves_only_itself(self, occupancy):
        """No identity, no tree: its repeats cannot be matched, so no copies are priced.

        Pricing an unprovable tree from an unknown would dress absence of evidence
        up as measurement — and two unhashed copies might be one tree, so even the
        copy count would be a guess.
        """
        first = occupancy.record(30, kind="rx", hops=4)
        second = occupancy.record(30, kind="rx", hops=4)
        totals = occupancy.window_airtime(60.0)
        assert totals["rx_floor_ms"] == pytest.approx(first + second)

    def test_tx_is_not_part_of_the_channel_floor(self, occupancy):
        """The floor answers what the channel carried; the tx share already answers the bot."""
        occupancy.record(100, kind="tx", sender="bot")
        assert occupancy.window_airtime(60.0)["rx_floor_ms"] == 0.0

    def test_the_floor_reports_as_its_own_percent(self, occupancy):
        occupancy.record(40, kind="rx", packet_hash="aabbccddeeff0011", hops=2)
        window = occupancy.windows()["window"]
        duration_ms = window["seconds"] * 1000.0
        assert window["rx_floor_percent"] == pytest.approx(window["rx_floor_ms"] / duration_ms * 100.0, abs=0.01)
        assert occupancy.fields()["window_rx_floor_percent"] == window["rx_floor_percent"]

    def test_parallel_copies_let_the_share_exceed_100_percent(self, occupancy):
        """Copies of 60 bytes over the default SF9 cost their airtime one by one: enough of
        them and the ten-second window costs more than it lasts.

        Two radios can put the same packet on air at the same moment out of each
        other's hearing — the CSMA paradox, sense is local, the channel is shared.
        The percent is the honest sum, not something to clamp at 100.
        """
        for index in range(40):
            occupancy.record(60, kind="rx", sender="alice", packet_hash=f"{index:016x}", hops=0)
        fast = occupancy.windows()["fast"]
        assert fast["rx_percent"] > 100.0
        assert fast["rx_floor_percent"] >= fast["rx_percent"]

    def test_fields_flatten_the_network_view_as_unknown_not_zero(self, occupancy):
        """The snapshot and the text format carry the counts; an unknown renders empty."""
        occupancy.record(30, kind="rx", sender="alice", hops=0)
        fields = occupancy.fields()
        assert fields["window_distinct_senders"] == 1
        assert fields["window_dup_factor"] is None
        assert fields["window_avg_hops"] == 0.0
        text = render_key_value(fields)
        assert "window_distinct_senders=1" in text
        assert "window_dup_factor=" in text  # empty: unknown, not 0 or 1
        assert "window_avg_hops=0" in text

    def test_network_view_survives_the_live_snapshot(self, occupancy, conn):
        """A viewer process reads the JSON, so dup_factor has to be in it."""
        occupancy.record(30, kind="rx", sender="alice", packet_hash="aabbccddeeff0011", at=1_700_000_000.0)
        occupancy.record(30, kind="rx", sender="bob", packet_hash="aabbccddeeff0011", at=1_700_000_001.0)
        occupancy.flush(conn)
        stored = json.loads(conn.execute(
            "SELECT value FROM bot_metadata WHERE key = ?", (LIVE_METADATA_KEY,)
        ).fetchone()[0])
        assert stored["window_dup_factor"] == 2.0
        assert stored["window_distinct_senders"] == 2

    def test_normalize_helpers_keep_the_same_rules(self):
        """record() normalizes, and the helpers state the rule once for every caller."""
        assert normalize_packet_hash(" AABBCCDDEEFF0011 ") == "aabbccddeeff0011"
        assert normalize_packet_hash(INVALID_PACKET_HASH) is None
        assert normalize_packet_hash("") is None
        assert normalize_packet_hash(None) is None
        assert normalize_hops(0) == 0
        assert normalize_hops("3") == 3
        assert normalize_hops(None) is None
        assert normalize_hops(-1) is None
        assert normalize_hops(300) is None


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
        parser.set("Data_Retention", "occupancy_emitters_retention_days", "90")
        parser.set("Data_Retention", "occupancy_relays_retention_days", "90")
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

    def test_emitters_past_retention_are_pruned(self, retention_config, conn):
        conn.execute(
            "INSERT INTO occupancy_emitters (emitter, airtime_ms, packets, first_seen, last_seen) "
            "VALUES ('62010a', 10.0, 1, ?, ?)",
            (1_700_000_000.0, 1_700_000_000.0),
        )
        conn.commit()
        self.Stub(retention_config, conn)._cleanup_occupancy()
        assert conn.execute("SELECT COUNT(*) FROM occupancy_emitters").fetchone()[0] == 0

    def test_relays_past_retention_are_pruned(self, retention_config, conn):
        conn.execute(
            "INSERT INTO occupancy_relays (relay, airtime_ms, packets, first_seen, last_seen) "
            "VALUES ('b06d1c', 10.0, 1, ?, ?)",
            (1_700_000_000.0, 1_700_000_000.0),
        )
        conn.commit()
        self.Stub(retention_config, conn)._cleanup_occupancy()
        assert conn.execute("SELECT COUNT(*) FROM occupancy_relays").fetchone()[0] == 0

    def test_day_rows_past_retention_are_pruned(self, retention_config, conn):
        """The day grain ages on the same keys as the ledger it sums into.

        A window the reader selects cannot reach further back than the station
        totals it describes, or the two views of one station disagree about how
        far the history goes.
        """
        now = int(occupancy_module.time.time())
        old_day = (now - 91 * 86400) // 86400 * 86400
        new_day = now // 86400 * 86400
        for table, key in (
            ("occupancy_sender_days", "sender"),
            ("occupancy_emitter_days", "emitter"),
            ("occupancy_relay_days", "relay"),
        ):
            for day in (old_day, new_day):
                conn.execute(
                    f"INSERT INTO {table} ({key}, day_ts, airtime_ms, packets, advert_ms, last_seen) "
                    "VALUES (?, ?, 10.0, 1, 0.0, ?)",
                    ("62010a", day, float(day)),
                )
        conn.commit()
        self.Stub(retention_config, conn)._cleanup_occupancy()
        for table in ("occupancy_sender_days", "occupancy_emitter_days", "occupancy_relay_days"):
            remaining = conn.execute(f"SELECT day_ts FROM {table}").fetchall()
            assert [row["day_ts"] for row in remaining] == [new_day]

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
        assert example.has_option("Data_Retention", "occupancy_emitters_retention_days")
        assert example.has_option("Data_Retention", "occupancy_relays_retention_days")


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

    def test_the_bot_pays_its_airtime_under_its_full_key(self, occupancy, conn):
        """The tx sender is the radio's full key, not the two-byte prefix.

        The senders table resolves names on the full key; a prefix could belong to
        several keys, and a row nobody can resolve is a name the page cannot honour.
        """
        from modules.transmission_tracker import TransmissionTracker

        full_key = "40accf24" + "0" * 56
        bot = _make_bot(occupancy)
        bot.meshcore.self_info = {"public_key": full_key.upper()}
        TransmissionTracker(bot).record_transmission("hello", "general", "channel")
        occupancy.flush(conn)
        senders = {row["sender"] for row in conn.execute("SELECT sender FROM occupancy_senders")}
        assert senders == {full_key}

    def test_the_packet_path_carries_the_hash_and_the_path(self, occupancy):
        """rf_log attaches the hash to the packet and the path to routing_info.

        A repeat heard through the flood router arrives twice with the same hash;
        if the capture path dropped the hash, the window could not see it.
        """
        integration = _integration(_make_bot(occupancy))
        for _ in range(2):
            integration._record_occupancy(
                {
                    "routing_info": {"payload_length": 30, "path_length": 2, "packet_hash": "aabbccddeeff0011"},
                    "sender_pubkey": "alice",
                },
                "rx",
            )
        totals = occupancy.window_airtime(60.0)
        assert totals["rx_hash_known"] == 2 and totals["rx_unique_packets"] == 1
        assert totals["rx_hops_known"] == 2 and totals["rx_hops_sum"] == 4

    def test_the_path_feeds_both_ends_of_the_ledgers(self, occupancy, conn):
        """One path, read from each end by the recorder: its last entry is the
        emitter, its first entry is where the copy entered the mesh.

        The capture path hands the path over once; splitting it into two facts is
        the recorder's job, and a copy heard without a path charges an emitter and
        no relay at all.
        """
        integration = _integration(_make_bot(occupancy))
        integration._record_occupancy(
            {
                "routing_info": {"payload_length": 30, "path_length": 2},
                "sender_pubkey": "alice",
                "path": ["B06D1C", "62010A"],
            },
            "rx",
        )
        integration._record_occupancy(
            {"routing_info": {"payload_length": 30, "path_length": 0}, "sender_pubkey": "bob"},
            "rx",
        )
        occupancy.flush(conn)
        assert {row["emitter"] for row in occupancy.top_emitters(conn)} == {"62010a", UNATTRIBUTED_SENDER}
        assert [row["relay"] for row in occupancy.top_relays(conn)] == ["b06d1c"]

    def test_a_direct_packet_transport_code_never_becomes_a_path_label(self, occupancy, conn):
        """A direct packet has no route info: what looks like a one-entry path is its
        transport codes, and reading a node out of them invents an identity.

        Verified on live traffic: one TRANSPORT_DIRECT ACK with path ["62016C"]
        wrote a phantom emitter and entry relay, and the two 6-char addresses it
        put under "6201" froze the prefix fold for the real node there.  The
        airtime is still charged — only the invented labels are refused.
        """
        integration = _integration(_make_bot(occupancy))
        key = "aabbccdd" + "0" * 56
        integration._record_occupancy(
            {
                "routing_info": {"payload_length": 30, "path_length": 1},
                "route_type_name": "TRANSPORT_DIRECT",
                "payload_type_name": "ACK",
                "sender_pubkey": key,
                "path": ["62016C"],
            },
            "rx",
        )
        occupancy.flush(conn)
        assert {row["emitter"] for row in occupancy.top_emitters(conn)} == {key}
        assert not list(occupancy.top_relays(conn))

    def test_a_named_flood_still_feeds_both_labels(self, occupancy, conn):
        """Gating the labels on flood route types must not lose the real flood path:
        a route_type_name carried by a flooded copy changes nothing."""
        integration = _integration(_make_bot(occupancy))
        integration._record_occupancy(
            {
                "routing_info": {"payload_length": 30, "path_length": 2},
                "route_type_name": "TRANSPORT_FLOOD",
                "sender_pubkey": "alice",
                "path": ["B06D1C", "62010A"],
            },
            "rx",
        )
        occupancy.flush(conn)
        assert {row["emitter"] for row in occupancy.top_emitters(conn)} == {"62010a"}
        assert [row["relay"] for row in occupancy.top_relays(conn)] == ["b06d1c"]

    def test_a_packet_without_path_info_keeps_the_counts_unknown(self, occupancy):
        """A packet that never decoded carries no hash and no path: the window stays silent,
        it does not report one shared identity or a zero-hop direct hear."""
        _integration(_make_bot(occupancy))._record_occupancy(
            {"routing_info": {"payload_length": 30, "packet_hash": "0000000000000000"}}, "rx"
        )
        totals = occupancy.window_airtime(60.0)
        assert totals["samples"] == 1
        assert totals["rx_hash_known"] == 0
        assert totals["rx_hops_known"] == 0

    def test_the_path_is_also_read_from_the_decoded_packet(self, occupancy):
        """Some packet shapes carry the path length on the packet, not in routing_info."""
        _integration(_make_bot(occupancy))._record_occupancy(
            {"routing_info": {"payload_length": 30}, "packet_hash": "11223344556677aa", "path_len": 1}, "rx"
        )
        totals = occupancy.window_airtime(60.0)
        assert totals["rx_hops_known"] == 1 and totals["rx_hops_sum"] == 1
        assert totals["rx_hash_known"] == 1

    def test_flush_skips_absent_tables(self, occupancy):
        """The writer may point at a database this process never migrated."""
        integration = _integration(_make_bot(occupancy))
        connection = sqlite3.connect(":memory:")
        occupancy.record(30, kind="rx")
        integration._flush_occupancy(connection)
        connection.close()

    def test_a_forced_flush_ends_the_grace_through_the_writer(self, occupancy, conn):
        """Shutdown flushes with force: the last write leaves nothing held in memory."""
        integration = _integration(_make_bot(occupancy))
        occupancy.record(30, kind="rx", sender=UNATTRIBUTED_SENDER, packet_hash="aabbccddeeff0011")
        integration._flush_occupancy(conn)
        assert not list(conn.execute("SELECT * FROM occupancy_sender_days"))
        integration._flush_occupancy(conn, force=True)
        rows = list(conn.execute("SELECT * FROM occupancy_sender_days"))
        assert len(rows) == 1
        assert rows[0]["sender"] == UNATTRIBUTED_SENDER

    def test_nothing_is_recorded_when_disabled(self):
        quiet = ChannelOccupancy(configparser.ConfigParser(), logging.getLogger("test"))
        _integration(_make_bot(quiet))._record_occupancy({"routing_info": {"payload_length": 30}}, "rx")
        assert quiet.fields()["samples"] == 0

    def test_an_advert_attributes_its_advertised_key(self, occupancy):
        """An advert puts the advertiser's full key at the front of its payload.

        Verified on live traffic: 299 of the last 300 adverts had a head key that
        resolves to a tracked contact, including those relayed on a transport
        channel — MeshCore signs adverts rather than encrypting them, so the
        transport codes say nothing about the payload being readable.
        """
        key = "12365aa9" + "0" * 56
        _integration(_make_bot(occupancy))._record_occupancy(
            {
                "routing_info": {"payload_length": 125, "path_length": 2},
                "payload_type_name": "ADVERT",
                "has_transport_codes": True,
                "payload_bytes": 117,
                "payload_hex": key + "ab" * 85,
            },
            "rx",
        )
        window = occupancy.windows()["window"]
        assert window["distinct_senders"] == 1
        assert window["unattributed_packets"] == 0

    def test_advert_and_message_packets_mark_the_sender_slices_apart(self, occupancy, conn):
        """The recorder splits presence traffic from talking traffic on the same row.

        The payload type, not the identity, decides the slice: an advert pays its
        advertiser's advert slice whatever route it arrived on, a message never does.
        """
        integration = _integration(_make_bot(occupancy))
        key = "12365aa9" + "0" * 56
        integration._record_occupancy(
            {
                "routing_info": {"payload_length": 125, "path_length": 2},
                "payload_type_name": "ADVERT",
                "payload_bytes": 117,
                "payload_hex": key + "ab" * 85,
            },
            "rx",
        )
        integration._record_occupancy(
            {
                "routing_info": {"payload_length": 30},
                "sender_pubkey": key,
                "payload_type_name": "GRP_TXT",
            },
            "rx",
        )
        occupancy.flush(conn)
        row = next(r for r in occupancy.top_senders(conn) if r["sender"] == key)
        assert 0.0 < row["advert_ms"] < row["airtime_ms"]

    def test_an_advert_payload_too_short_to_hold_a_key_gives_no_identity(self, occupancy):
        """The size floor is what keeps the rule honest.

        Without it the head bytes of anything hex-shaped become a station, and an
        invented identity in a table read as evidence is exactly what the honesty
        rule refuses.
        """
        key = "12365aa9" + "0" * 56
        _integration(_make_bot(occupancy))._record_occupancy(
            {
                "routing_info": {"payload_length": 20},
                "payload_type_name": "ADVERT",
                "payload_bytes": 20,
                "payload_hex": key[:40],
            },
            "rx",
        )
        window = occupancy.windows()["window"]
        assert window["distinct_senders"] == 0
        assert window["unattributed_packets"] == 1

    def test_heard_traffic_without_an_identity_goes_to_the_unattributed_bucket(self, occupancy):
        """The airtime is never dropped for want of a name — only the identity is."""
        _integration(_make_bot(occupancy))._record_occupancy(
            {"routing_info": {"payload_length": 60, "packet_hash": "aabbccddeeff0011"}, "payload_type_name": "GRP_TXT"},
            "rx",
        )
        window = occupancy.windows()["window"]
        assert window["unattributed_packets"] == 1
        assert window["rx_ms"] > 0.0

    def test_a_short_path_prefix_is_not_made_into_a_sender(self, occupancy):
        """A two-byte prefix is not an identity: it belongs to several keys.

        The old fallback keyed rx on the routing prefix, which reads like a station
        in the senders table but cannot be resolved to one. An admitted unknown
        beats a wrong name.
        """
        _integration(_make_bot(occupancy))._record_occupancy(
            {"routing_info": {"payload_length": 30, "from_prefix": "40"}}, "rx"
        )
        window = occupancy.windows()["window"]
        assert window["distinct_senders"] == 0
        assert window["unattributed_packets"] == 1


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


def _set_occupancy_enabled(config_path, value):
    """Rewrite the setting in the file, as an editor or the web admin would."""
    parser = configparser.ConfigParser()
    parser.read(config_path, encoding="utf-8")
    parser.set("Channel_Occupancy", "enabled", value)
    with open(config_path, "w", encoding="utf-8") as handle:
        parser.write(handle)


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

    def test_the_days_param_shows_the_window_not_the_lifetime_totals(self, occupancy, conn, viewer):
        """The history selector governs the two tables too.

        With `days` the rows come from the day tables summed over the window;
        without it the cumulative ledgers answer, as they always did.  A station
        busy three weeks ago and silent this week must not sit at the top of a
        table the reader selected as "last 24 hours".
        """
        day = int(occupancy_module.time.time()) // 86400 * 86400
        occupancy.record(40, kind="rx", sender="w", emitter="62010a", at=day - 3 * 86400)
        occupancy.record(30, kind="rx", sender="w", emitter="62010a", at=day + 5)
        occupancy.flush(conn)
        client = viewer.app.test_client()
        cumulative = json.loads(client.get("/api/telemetry/occupancy").get_data())
        assert "share_window_days" not in cumulative
        assert cumulative["top_senders"][0]["packets"] == 2
        windowed = json.loads(client.get("/api/telemetry/occupancy?days=1").get_data())
        assert windowed["share_window_days"] == 1
        assert [row["sender"] for row in windowed["top_senders"]] == ["w"]
        assert windowed["top_senders"][0]["packets"] == 1
        assert [row["emitter"] for row in windowed["top_emitters"]] == ["62010a"]
        assert windowed["top_emitters"][0]["packets"] == 1

    def test_days_outside_the_supported_range_are_clamped_not_rejected(self, occupancy, conn, viewer):
        """`days=0` or `days=3650` cannot mean an empty table or an unbounded scan."""
        occupancy.record(40, kind="rx", sender="w")
        occupancy.flush(conn)
        client = viewer.app.test_client()
        assert json.loads(client.get("/api/telemetry/occupancy?days=0").get_data())["share_window_days"] == 1
        assert json.loads(client.get("/api/telemetry/occupancy?days=3650").get_data())["share_window_days"] == 90

    def test_the_short_address_folds_into_the_only_longer_one(self, occupancy, conn, viewer):
        """`6201` and `62010a` are one relay seen at two widths, not two emitters.

        MeshCore writes a relay's node address 2 bytes wide in short packets and 3
        bytes wide in extended ones, so the same antenna lands in the ledger twice.
        The fold is allowed precisely because the ledger knows a single longer
        address under the short one — the same reasoning that lets a prefix become
        a name only when exactly one contact matches it.
        """
        base = int(occupancy_module.time.time()) // 60 * 60
        occupancy.record(40, kind="rx", sender="w", emitter="62010a", at=base)
        occupancy.record(30, kind="rx", sender="w", emitter="6201", at=base + 10)
        occupancy.flush(conn)
        payload = json.loads(viewer.app.test_client().get("/api/telemetry/occupancy").get_data())
        rows = {row["emitter"]: row for row in payload["top_emitters"]}
        assert set(rows) == {"62010a"}
        assert rows["62010a"]["packets"] == 2
        folded = rows["62010a"]["merged_prefixes"]
        assert folded[0]["label"] == "6201"
        assert folded[0]["packets"] == 1

    def test_the_fold_stops_when_the_ledger_knows_two_longer_addresses(self, occupancy, conn, viewer):
        """With `62010a` and `62014c` both heard, the 2-byte copies cannot be split.

        Folding them into either row would attribute copies to a coin flip — a
        station the wire did not name — so the short row stays on its own.
        """
        base = int(occupancy_module.time.time()) // 60 * 60
        occupancy.record(40, kind="rx", sender="w", emitter="62010a", at=base)
        occupancy.record(30, kind="rx", sender="w", emitter="62014c", at=base + 5)
        occupancy.record(20, kind="rx", sender="w", emitter="6201", at=base + 10)
        occupancy.flush(conn)
        payload = json.loads(viewer.app.test_client().get("/api/telemetry/occupancy").get_data())
        rows = {row["emitter"]: row for row in payload["top_emitters"]}
        assert set(rows) == {"62010a", "62014c", "6201"}
        assert all("merged_prefixes" not in row for row in payload["top_emitters"])

    def test_the_fold_applies_to_windowed_rows_too(self, occupancy, conn, viewer):
        """The period view folds the same way: one relay, one row inside the window."""
        day = int(occupancy_module.time.time()) // 86400 * 86400
        occupancy.record(40, kind="rx", sender="w", emitter="62010a", at=day + 10)
        occupancy.record(30, kind="rx", sender="w", emitter="6201", at=day + 20)
        occupancy.flush(conn)
        payload = json.loads(viewer.app.test_client().get("/api/telemetry/occupancy?days=1").get_data())
        rows = {row["emitter"]: row for row in payload["top_emitters"]}
        assert set(rows) == {"62010a"}
        assert rows["62010a"]["packets"] == 2
        assert rows["62010a"]["merged_prefixes"][0]["label"] == "6201"

    def test_a_fold_target_outside_the_fetched_page_still_folds_the_short_row(self, occupancy, conn, viewer):
        """One big short row with its tiny longer partner below the page limit.

        Live shape: 1,945 copies under `6201`, 589 under `62010a`, and on the
        relay ledger the partner row held 18 copies — below the top-10 page the
        endpoint first drew, so the fold had nothing to fold INTO and the table
        showed a bare prefix where a named antenna belonged.  The endpoint now
        draws a wide pool before folding; the unit path below covers the case
        where even the pool misses the partner.
        """
        base = int(occupancy_module.time.time()) // 60 * 60
        occupancy.record(400, kind="rx", sender="w", emitter="6201", at=base)
        occupancy.record(8, kind="rx", sender="w", emitter="62010a", at=base + 5)
        for i in range(12):
            occupancy.record(150, kind="rx", sender="w", emitter=f"{i:04x}", at=base + 10)
        occupancy.flush(conn)
        payload = json.loads(viewer.app.test_client().get("/api/telemetry/occupancy").get_data())
        rows = {row["emitter"]: row for row in payload["top_emitters"]}
        assert "6201" not in rows
        folded = rows["62010a"]
        assert folded["packets"] == 2
        assert folded["merged_prefixes"][0]["label"] == "6201"

    def test_the_fold_relabels_without_summing_an_unseen_partner(self, conn):
        """Unit path: when the fetched rows do not hold the partner at all, the
        short row is relabelled to it — the prefix can only mean that address —
        but its airtime is not conjured in, because this fetch never saw it.
        """
        from modules.web_viewer.app import _fold_prefix_rows

        conn.execute(
            "INSERT INTO occupancy_emitters (emitter, airtime_ms, packets, first_seen, last_seen)"
            " VALUES ('62010a', 900.0, 9, 1.0, 2.0)"
        )
        rows = [{"emitter": "6201", "airtime_ms": 120.0, "packets": 1}]
        _fold_prefix_rows(conn, rows, "emitter", "occupancy_emitters")
        assert len(rows) == 1
        assert rows[0]["emitter"] == "62010a"
        assert rows[0]["packets"] == 1
        assert rows[0]["airtime_ms"] == 120.0
        assert rows[0]["merged_prefixes"] == [{"label": "6201", "packets": 1, "airtime_ms": 120.0}]

    def test_the_airtime_rows_carry_their_share_of_the_channel_capacity(self, occupancy, conn, viewer):
        """Each row reports its distance to channel saturation, not its share of the others.

        The ceiling is what the medium could physically carry over the selected
        period — one channel-second per elapsed second — so the percentages are
        tiny on a real ledger and are read against 100%, not against each other.
        The expectations come from the payload's own ``period_capacity_ms``
        because the window's elapsed time depends on when the request runs.
        """
        day = int(occupancy_module.time.time()) // 86400 * 86400
        occupancy.record(30, kind="rx", sender="w", at=day + 5)
        occupancy.record(30, kind="rx", sender="w", at=day + 6)
        occupancy.record(30, kind="rx", sender="v", at=day + 7)
        occupancy.record(30, kind="rx", sender="u", at=day - 3 * 86400)
        occupancy.flush(conn)
        client = viewer.app.test_client()
        windowed = json.loads(client.get("/api/telemetry/occupancy?days=1").get_data())
        rows = {row["sender"]: row for row in windowed["top_senders"]}
        assert "u" not in rows
        # The day-aligned "last 1 day" window has been running for between one
        # and two days; that elapsed time, not the ledger's traffic, is the
        # denominator.
        capacity = windowed["period_capacity_ms"]
        assert 86_400_000 <= capacity < 172_800_000
        # The page states the span it answers for, so the percent can be checked.
        assert windowed["period_start_ts"] == occupancy.window_cutoff_day(1)
        assert rows["w"]["capacity_percent"] == round(60.0 / capacity * 100.0, 1)
        assert rows["v"]["capacity_percent"] == round(30.0 / capacity * 100.0, 1)
        cumulative = json.loads(client.get("/api/telemetry/occupancy").get_data())
        rows = {row["sender"]: row for row in cumulative["top_senders"]}
        # The cumulative range starts at the oldest recorded station, three
        # days back, so its ceiling is wider than the 1-day window's.
        capacity = cumulative["period_capacity_ms"]
        assert capacity >= 3 * 86_400_000
        assert cumulative["period_start_ts"] == float(day - 3 * 86400)
        for sender, airtime in (("w", 60.0), ("v", 30.0), ("u", 30.0)):
            assert rows[sender]["capacity_percent"] == round(airtime / capacity * 100.0, 1)

    def test_the_capacity_is_one_ceiling_shared_by_all_three_ledgers(self, occupancy, conn, viewer):
        """Emitter-labelled traffic with no flood path (no entry relay) still
        prices itself against the channel's one ceiling while the relay ledger
        stays empty — the ceiling is a property of the medium, not of any one
        ledger, and the three ledgers answer for different facts."""
        base = int(occupancy_module.time.time()) // 60 * 60
        occupancy.record(30, kind="rx", sender="w", emitter="62010a", at=base)
        occupancy.flush(conn)
        payload = json.loads(viewer.app.test_client().get("/api/telemetry/occupancy").get_data())
        capacity = payload["period_capacity_ms"]
        assert capacity > 0
        emitter = payload["top_emitters"][0]
        assert emitter["capacity_percent"] == round(emitter["airtime_ms"] / capacity * 100.0, 1)
        assert payload["top_relays"] == []

    def test_the_capacity_is_unknown_when_nothing_is_recorded(self, conn):
        """With no recorded moment anywhere, the cumulative period's range is
        unknown, so its start and ceiling are both ``None`` — the page shows a
        dash rather than a percent of an undated range."""
        from modules.web_viewer.app import _period_window

        assert _period_window(conn, None) == (None, None)
        # A window's span is a property of the clock, not of the ledger, so it
        # is known even over an empty database.
        day = int(occupancy_module.time.time()) // 86400 * 86400
        start, capacity = _period_window(conn, day)
        assert start == float(day)
        assert capacity > 0

    def test_each_table_header_sums_its_whole_ledger_for_the_period(self, occupancy, conn, viewer):
        """The recap sums the ledger, not the page's slice; one sum per ledger is
        never a sum across ledgers, and an empty ledger sums to None, not 0."""
        day = int(occupancy_module.time.time()) // 86400 * 86400
        # record() prices payload bytes through the LoRa estimate, so the sum is
        # asserted in that same unit, not in invented round numbers.
        unit = lora_airtime_ms(30, **occupancy.modulation())
        occupancy.record(30, kind="rx", sender="w", at=day + 5)
        occupancy.record(30, kind="rx", sender="w", at=day + 6)
        occupancy.record(30, kind="rx", sender="v", at=day + 7)
        occupancy.record(30, kind="rx", sender="u", at=day - 3 * 86400)
        occupancy.flush(conn)
        client = viewer.app.test_client()
        windowed = json.loads(client.get("/api/telemetry/occupancy?days=1").get_data())
        totals = windowed["period_totals"]
        # The window total is every in-window row, outside-window senders excluded,
        # and it prices the same channel ceiling as the rows do.
        assert totals["senders"]["airtime_ms"] == pytest.approx(3 * unit)
        assert totals["senders"]["packets"] == 3
        assert totals["senders"]["stations"] == 2
        assert totals["senders"]["capacity_percent"] == round(
            3 * unit / windowed["period_capacity_ms"] * 100.0, 1)
        # A ledger with no row for the period has no total: None, not a measured 0.
        assert totals["relays"]["airtime_ms"] is None
        assert totals["relays"]["capacity_percent"] is None
        assert totals["relays"]["stations"] == 0
        cumulative = json.loads(client.get("/api/telemetry/occupancy").get_data())
        totals = cumulative["period_totals"]
        assert totals["senders"]["airtime_ms"] == pytest.approx(4 * unit)
        assert totals["senders"]["stations"] == 3
        assert totals["senders"]["capacity_percent"] == round(
            4 * unit / cumulative["period_capacity_ms"] * 100.0, 1)

    def test_the_recap_advert_slice_stays_unknown_when_the_period_mixes_eras(self, conn):
        """Rows written before the advert split know no advert share, so a
        period mixing both eras sums to unknown, not to a share over only the
        rows that happen to know it."""
        from modules.web_viewer.app import _ledger_period_totals

        conn.execute(
            "INSERT INTO occupancy_senders (sender, airtime_ms, packets, first_seen, last_seen)"
            " VALUES ('w', 100.0, 1, 1.0, 2.0)"
        )
        conn.execute(
            "INSERT INTO occupancy_senders (sender, airtime_ms, packets, first_seen, last_seen, advert_ms)"
            " VALUES ('v', 100.0, 1, 1.0, 2.0, 25.0)"
        )
        totals = _ledger_period_totals(conn, None, 1000.0)["senders"]
        assert totals["advert_percent"] is None
        assert totals["airtime_ms"] == 200.0
        assert totals["capacity_percent"] == 20.0

    def test_a_folded_row_prices_one_merged_cost(self, occupancy, conn, viewer):
        """The fold happens before the percent, so the merged antenna carries one
        percent for all its copies, not two competing ones."""
        base = int(occupancy_module.time.time()) // 60 * 60
        occupancy.record(120, kind="rx", sender="w", emitter="6201", at=base)
        occupancy.record(30, kind="rx", sender="w", emitter="62010a", at=base + 5)
        occupancy.flush(conn)
        payload = json.loads(viewer.app.test_client().get("/api/telemetry/occupancy").get_data())
        rows = {row["emitter"]: row for row in payload["top_emitters"]}
        assert set(rows) == {"62010a"}
        capacity = payload["period_capacity_ms"]
        assert capacity > 0
        assert rows["62010a"]["capacity_percent"] == round(150.0 / capacity * 100.0, 1)

    def test_top_senders_resolve_full_keys_to_contact_names(self, occupancy, conn, viewer):
        """A full public key in the table gets its contact name; the unattributed bucket does not.

        The page names stations from the contact tracking, and a bucket that stands
        for "no identity" must stay labelled as one — resolving it to a name would
        invent the very station the honesty rule refuses.
        """
        full_key = "12365aa9" + "0" * 56
        conn.execute(
            "INSERT INTO complete_contact_tracking (public_key, name, role) VALUES (?, ?, ?)",
            (full_key, "RPT_TEST", "repeater"),
        )
        conn.commit()
        base = int(occupancy_module.time.time()) // 60 * 60
        occupancy.record(40, kind="rx", sender=full_key, at=base)
        occupancy.record(30, kind="rx", sender=UNATTRIBUTED_SENDER, at=base + 5)
        occupancy.flush(conn)
        payload = json.loads(viewer.app.test_client().get("/api/telemetry/occupancy").get_data())
        rows = {row["sender"]: row for row in payload["top_senders"]}
        assert rows[full_key]["name"] == "RPT_TEST"
        assert rows[full_key]["short"] == full_key[:6]
        assert rows[full_key]["unattributed"] is False
        assert rows[UNATTRIBUTED_SENDER]["name"] is None
        assert rows[UNATTRIBUTED_SENDER]["unattributed"] is True

    def test_top_emitters_resolve_only_unique_prefixes(self, occupancy, conn, viewer):
        """A flood-path node address becomes a name when exactly one tracked contact
        matches it — and stays bytes when it could belong to several.

        The wire carries 2 to 3 leading bytes of a relay's key.  Six hex digits match
        thousands of possible keys cryptographically, but among the contacts this bot
        actually tracks the question "which one?" has a definite answer only when one
        matches; with two, naming either would be a coin flip presented as a fact.
        """
        unique_key = "12365a" + "0" * 58
        ambiguous_a = "aabbcc" + "00" + "0" * 56
        ambiguous_b = "aabbcc" + "11" + "1" * 56
        for key, name in ((unique_key, "G2PV_TEST"), (ambiguous_a, "Amber"), (ambiguous_b, "Azure")):
            conn.execute(
                "INSERT INTO complete_contact_tracking (public_key, name, role) VALUES (?, ?, ?)",
                (key, name, "repeater"),
            )
        conn.commit()
        base = int(occupancy_module.time.time()) // 60 * 60
        occupancy.record(40, kind="rx", sender="writer", emitter="12365a", at=base)
        occupancy.record(40, kind="rx", sender="writer", emitter="aabbcc", at=base)
        occupancy.record(30, kind="rx", sender=UNATTRIBUTED_SENDER, emitter=UNATTRIBUTED_SENDER, at=base + 5)
        occupancy.flush(conn)
        payload = json.loads(viewer.app.test_client().get("/api/telemetry/occupancy").get_data())
        rows = {row["emitter"]: row for row in payload["top_emitters"]}
        assert rows["12365a"]["name"] == "G2PV_TEST"
        assert rows["12365a"]["short"] == "12365a"
        assert rows["aabbcc"]["name"] is None
        assert rows["aabbcc"]["short"] == "aabbcc"
        assert rows[UNATTRIBUTED_SENDER]["unattributed"] is True
        assert 0.0 <= rows["12365a"]["advert_percent"] <= 100.0

    def test_the_advert_share_is_a_percent_or_unknown_never_an_invented_zero(self, occupancy, conn, viewer):
        """The API carries the advert slice as a percent of the sender's own airtime.

        A sender recorded before the split existed reports None — the page renders a
        dash — because a 0% there would claim a station that never advertised, and
        nobody watched whether it did.
        """
        full_key = "12365aa9" + "0" * 56
        base = int(occupancy_module.time.time()) // 60 * 60
        occupancy.record(125, kind="rx", sender=full_key, advert=True, at=base)
        occupancy.record(30, kind="rx", sender=full_key, advert=False, at=base + 1)
        occupancy.flush(conn)
        conn.execute(
            "INSERT INTO occupancy_senders (sender, airtime_ms, packets, first_seen, last_seen) "
            "VALUES ('legacy', 100.0, 3, 1.0, 2.0)"
        )
        conn.commit()
        payload = json.loads(viewer.app.test_client().get("/api/telemetry/occupancy").get_data())
        rows = {row["sender"]: row for row in payload["top_senders"]}
        assert 0.0 < rows[full_key]["advert_percent"] < 100.0
        assert rows["legacy"]["advert_percent"] is None

    def test_enabled_is_the_setting_now_not_the_one_the_snapshot_was_written_under(self, occupancy, conn, viewer):
        """The page tells the reader to set [Channel_Occupancy] enabled, so it has to report
        the setting in force rather than the value a snapshot happens to carry.

        Recording may have been on when the snapshot was written and off now. Those are two
        facts: a payload that reports only the stored one tells the reader to turn on what is
        already on, or presents historical numbers as current ones. The file is flipped, not
        the viewer's copy: the viewer keeps a boot copy and a hot config reload never
        reaches it, and the file is what governs the recording either way.
        """
        _write_snapshot(occupancy, conn)
        client = viewer.app.test_client()

        def get_payload():
            return json.loads(client.get("/api/telemetry/occupancy").get_data())

        assert get_payload()["enabled"] is True
        _set_occupancy_enabled(viewer.config_path, "false")
        payload = get_payload()
        assert payload["enabled"] is False
        assert payload["snapshot_enabled"] is True
        # The figures still stand for the window they were recorded in.
        assert payload["window_rx_percent"] > 0.0
        # And turning the file back on is visible on the next request, without a restart.
        _set_occupancy_enabled(viewer.config_path, "true")
        payload = get_payload()
        assert payload["enabled"] is True
        assert "snapshot_enabled" not in payload

    def test_an_unreadable_config_file_falls_back_to_the_boot_copy(self, occupancy, conn, viewer):
        """The reader cannot open the file: the honest answer is the setting as this process
        knows it from boot, not an invented off that would tell the reader to change a
        setting the reader may well have already set.
        """
        _write_snapshot(occupancy, conn)
        viewer.config_path = viewer.config_path + ".missing"
        payload = json.loads(viewer.app.test_client().get("/api/telemetry/occupancy").get_data())
        assert payload["enabled"] is True
        assert "snapshot_enabled" not in payload

    def test_a_config_file_without_the_section_reports_off(self, occupancy, conn, viewer):
        """No section and no flag is the writer's default: off. The stored snapshot still
        says what it was written under."""
        _write_snapshot(occupancy, conn)
        parser = configparser.ConfigParser()
        parser.read(viewer.config_path, encoding="utf-8")
        parser.remove_section("Channel_Occupancy")
        with open(viewer.config_path, "w", encoding="utf-8") as handle:
            parser.write(handle)
        payload = json.loads(viewer.app.test_client().get("/api/telemetry/occupancy").get_data())
        assert payload["enabled"] is False
        assert payload["snapshot_enabled"] is True

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

    def test_history_carries_the_equal_period_before(self, occupancy, conn, viewer):
        """The reader comparing periods must not have to fetch twice, and the
        previous period arrives as its own object: an empty one is absent
        evidence, and only the reader who sees the range can say so."""
        _write_snapshot(occupancy, conn)
        payload = json.loads(viewer.app.test_client().get("/api/telemetry/occupancy/history?days=30").get_data())
        previous = payload["previous"]
        assert previous["end_time"] == payload["start_time"]
        assert previous["start_time"] == payload["start_time"] - 30 * 86400
        # Nothing was recorded before the snapshot's bucket: absent, not quiet.
        assert previous["buckets"] == []
        # The snapshot's bucket knows the split (its packet carried no identity,
        # so the share is unknown, while the column is not NULL): the reader can
        # tell "measured, no identified traffic" from "not recorded".
        assert payload["buckets"][0]["rx_hashed_ms"] == 0.0
        assert payload["copy_percent"] is None

    def test_history_text_format_states_the_previous_period(self, occupancy, conn, viewer):
        _write_snapshot(occupancy, conn)
        text = viewer.app.test_client().get(
            "/api/telemetry/occupancy/history?days=30&format=text").get_data(as_text=True)
        parsed = dict(line.split("=", 1) for line in text.splitlines())
        assert parsed["previous_buckets"] == "0"
        assert "copy_percent" in parsed

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
