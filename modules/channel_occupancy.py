#!/usr/bin/env python3
"""Channel occupancy: airtime per packet, rolling windows, and bucketed history.

Two numbers are kept separate and never merged:

* **rx share** — airtime this node *hears*. A node only hears part of the channel
  (hidden nodes, antenna gain, spatial reuse), so a percentage measured here is a
  local observation, not the airtime of the whole channel.
* **tx share** — airtime the bot *spends* itself, which is the only part it can
  decide about.

Airtime is computed from the modulation the mesh is configured with, using the
Semtech AN1200.13 formula. It is an estimate, not a measurement: nothing here
knows whether the channel was actually busy, only how long a packet of a given
size occupies it at a given SF/BW/CR. Real occupancy (CAD/LBT) belongs to a radio
driver, and the bot is a client of one over serial, BLE or TCP.

Conventions carried from `modules/web_viewer/dashboard_stats.py`:

* **NULL is not zero.** A bucket with no packet is absent, not `0.0`; rendering a
  missing bucket as zero produces a fake cliff at the retention boundary that reads
  as an outage.
* **Sums and counts, never means.** Any window re-aggregates correctly from sums.
* **Two clocks.** Rolling windows use `time.monotonic()` (a wall-clock jump from an
  NTP correction or a clock-sync command would stretch or collapse a window);
  buckets and rows store UTC epoch seconds, which is what the readers query.
"""

from __future__ import annotations

import configparser
import json
import logging
import math
import sqlite3
import time
from collections import deque
from typing import Any, Optional

SECTION = "Channel_Occupancy"

# Metadata key holding the live windows.  The windows live in the bot process, and
# the web viewer is a separate process: it can only read what the writer left
# behind, so the snapshot is stored rather than recomputed.
LIVE_METADATA_KEY = "occupancy.live"

# MeshCore defaults, used only when the device does not report its modulation.
DEFAULT_SF = 9
DEFAULT_BW_KHZ = 125
DEFAULT_CR_DEN = 5
DEFAULT_PREAMBLE = 8

# Semtech recommends low-data-rate optimization from a symbol time of 16.38 ms.
LDRA_SYMBOL_MS = 16.38

# Window names are part of the API contract and of the key=value output.
WINDOW_NAMES = ("fast", "window", "slow")


def normalize_bw_khz(bandwidth: Any) -> float:
    """Accept a bandwidth in kHz or in Hz and return kHz.

    The device reports kHz (125, 250, 500); a configuration written in Hz (125000)
    is the same value. Without this, 125000 read as kHz gives an airtime a thousand
    times too small, which looks like an idle channel rather than a wrong unit.
    """
    try:
        value = float(bandwidth)
    except (TypeError, ValueError):
        return DEFAULT_BW_KHZ
    if value <= 0:
        return DEFAULT_BW_KHZ
    return value / 1000.0 if value > 2000.0 else value


def normalize_cr_den(coding_rate: Any) -> int:
    """Return the coding-rate denominator (5..8) from either notation.

    AN1200.13 indexes the rate (1..4); the device reports the denominator (5..8).
    The multiplier used by the symbol count is the denominator itself, so the two
    notations must not be confused: index 3 (4/7) read as a denominator gives a
    symbol count built for 4/3, which is not a coding rate at all.
    """
    try:
        value = int(coding_rate)
    except (TypeError, ValueError):
        return DEFAULT_CR_DEN
    if 5 <= value <= 8:
        return value
    if 1 <= value <= 4:
        return value + 4
    return DEFAULT_CR_DEN


def lora_airtime_ms(
    payload_bytes: int,
    sf: int = DEFAULT_SF,
    bw_khz: float = DEFAULT_BW_KHZ,
    cr_den: int = DEFAULT_CR_DEN,
    preamble: int = DEFAULT_PREAMBLE,
    crc: bool = True,
    explicit_header: bool = True,
    ldra: Optional[bool] = None,
) -> float:
    """Return the time on air of a LoRa packet, in milliseconds.

    Symbol time is ``2^SF / BW``; the preamble is ``preamble + 4.25`` symbols (the
    4.25 is the sync pattern, which exists even with a zero-length preamble). The
    payload symbol count is the AN1200.13 expression: 8 fixed symbols plus the
    coded bits, where ``+16`` is the 16-bit CRC, ``-20*H`` is the 20-bit difference
    between implicit and explicit header mode, and ``4*(SF - 2*DE)`` is the number of
    bits per symbol with low-data-rate optimization.

    ``ldra=None`` applies the datasheet's auto rule (symbol time >= 16.38 ms), which
    is what a real radio does; passing False or True is the register value.

    Verified against an independent published calculator: SF12/125 kHz/CR 4/5, PL 64,
    explicit header, DE on, preamble 8 gives 2793.472 ms; SF12/500 kHz, PL 8, CR 4/5,
    implicit header, preamble 6, DE off gives 190.464 ms.
    """
    sf = max(6, min(int(sf), 12))
    bw_khz = float(bw_khz) or DEFAULT_BW_KHZ
    cr_den = normalize_cr_den(cr_den)
    preamble = max(0, min(int(preamble), 65535))

    symbol_ms = (2.0 ** sf) / bw_khz
    de = 1 if (symbol_ms >= LDRA_SYMBOL_MS if ldra is None else bool(ldra)) else 0

    pl = max(0, int(payload_bytes))
    bits = 8 * pl - 4 * sf + 28 + (16 if crc else 0) - (0 if explicit_header else 20)
    symbols = 8 + max(int(math.ceil(bits / (4 * (sf - 2 * de)))) * cr_den, 0)

    return (preamble + 4.25) * symbol_ms + symbols * symbol_ms


def nearest_rank_percentile(values: list[float], quantile: float) -> Optional[float]:
    """Nearest-rank percentile over bucket values.

    Nearest-rank on purpose: the linear-interpolated percentile under-reports the
    tail on a handful of samples, and the tail (a burst) is exactly what this is
    looked at. The resolution of the answer is the resolution of the samples, so a
    percentile over 60-second buckets is not the same measurement as a percentile
    over the 10-second instant windows.
    """
    samples = [float(v) for v in values if v is not None]
    if not samples:
        return None

    q = max(0.0, min(float(quantile), 1.0))
    rank = max(1, int(math.ceil(len(samples) * q)))
    return sorted(samples)[rank - 1]


def render_key_value(fields: dict[str, Any]) -> str:
    """Render fields as ``key=value`` lines for a monitoring agent.

    Booleans become 0/1, collections become comma lists, empty values stay empty,
    and a value containing whitespace is single-quoted so one field per line is
    still readable by a shell.
    """
    lines: list[str] = []
    for key, value in fields.items():
        if isinstance(value, bool):
            rendered = "1" if value else "0"
        elif isinstance(value, (list, tuple, set)):
            rendered = ",".join(str(item) for item in value)
        elif value is None:
            rendered = ""
        else:
            rendered = str(value)
        if any(char in rendered for char in (" ", "\t", "\n")):
            rendered = "'" + rendered.replace("'", "") + "'"
        lines.append(f"{key}={rendered}")
    return "\n".join(lines)


class ChannelOccupancy:
    """Rolling airtime windows, bucketed samples, and the per-sender share."""

    def __init__(
        self,
        config: configparser.ConfigParser,
        logger: logging.Logger,
    ) -> None:
        self.config = config
        self.logger = logger

        self.enabled = self._bool("enabled", False)
        self.sample_seconds = self._int("sample_seconds", 60)
        self.max_samples = self._int("max_samples", 5000)
        self.half_life_seconds = self._float("half_life_seconds", 300.0)
        self.window_seconds = {
            "fast": self._float("fast_window_seconds", 10.0),
            "window": self._float("window_seconds", 60.0),
            "slow": self._float("slow_window_seconds", 900.0),
        }
        self.quantiles = (0.5, 0.95)

        # Modulation. The device values win over the configured ones when the device
        # reports them, because a bot configured for SF9 on a mesh running SF11
        # reports an airtime that no packet on the channel has ever had.
        self.fallback_sf = self._int("spreading_factor", DEFAULT_SF)
        self.fallback_bw_khz = normalize_bw_khz(self._float("bandwidth_khz", DEFAULT_BW_KHZ))
        self.fallback_cr_den = normalize_cr_den(self._int("coding_rate", DEFAULT_CR_DEN))
        self.fallback_preamble = self._int("preamble_symbols", DEFAULT_PREAMBLE)
        self.use_device_modulation = self._bool("use_device_modulation", True)
        self.device_modulation: dict[str, Any] = {}

        # Ring buffer of samples: (monotonic, airtime_ms, kind). Bounded, otherwise a
        # busy mesh grows it for the whole process lifetime.
        self._samples: deque[tuple[float, float, str]] = deque(maxlen=self.max_samples)
        self._bucket: dict[str, Any] = {}
        # Pending per-sender increments since the last flush: airtime_ms and packets.
        # Kept as increments rather than totals because the flush writes deltas, and
        # writing a total on top of a stored total would double-count.
        self._senders: dict[str, list[float]] = {}

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _get(self, key: str, default: Any) -> Any:
        if not self.config.has_section(SECTION):
            return default
        return self.config.get(SECTION, key, fallback=str(default))

    def _int(self, key: str, default: int) -> int:
        try:
            return int(self._get(key, default))
        except (TypeError, ValueError):
            return default

    def _float(self, key: str, default: float) -> float:
        try:
            return float(self._get(key, default))
        except (TypeError, ValueError):
            return default

    def _bool(self, key: str, default: bool) -> bool:
        value = str(self._get(key, "true" if default else "false")).strip().lower()
        return value in ("1", "true", "yes", "on")

    def modulation(self) -> dict[str, Any]:
        """Return the modulation used for the next estimate."""
        device = self.device_modulation if self.use_device_modulation else {}
        sf = int(device.get("sf") or self.fallback_sf)
        bw = normalize_bw_khz(device.get("bw_khz") or self.fallback_bw_khz)
        cr = normalize_cr_den(device.get("cr_den") or self.fallback_cr_den)
        return {"sf": sf, "bw_khz": bw, "cr_den": cr, "preamble": self.fallback_preamble}

    def set_device_modulation(self, info: dict[str, Any]) -> None:
        """Store the modulation the device reports (0 means unknown)."""
        self.device_modulation = {
            "sf": int(info.get("sf") or 0),
            "bw_khz": normalize_bw_khz(info.get("bw")),
            "cr_den": normalize_cr_den(info.get("cr")),
        }

    def refresh_from_device(self, device_info: Any) -> bool:
        """Read the modulation from a device-info dict or object, keeping the last known value.

        Kept as a separate step from `set_device_modulation` because the caller runs it
        once per write batch, not once per packet: a per-packet getattr on the device
        object costs nothing on its own but adds a second way for the estimate to drift
        from what the radio is actually using.  A device that has not reported yet, or
        that reports 0, leaves the previous values in place rather than zeroing them —
        SF 0 is not a spreading factor, and an airtime built from it is not small, it is
        meaningless.
        """
        if device_info is None:
            return False

        def read(key: str) -> Any:
            if isinstance(device_info, dict):
                return device_info.get(f"radio_{key}", device_info.get(key))
            return getattr(device_info, f"radio_{key}", getattr(device_info, key, None))

        sf = int(read("sf") or 0)
        if not sf:
            return False

        self.set_device_modulation({"sf": sf, "bw": read("bw"), "cr": read("cr")})
        return True

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record(
        self,
        payload_bytes: int,
        *,
        kind: str = "rx",
        sender: Optional[str] = None,
        at: Optional[float] = None,
    ) -> float:
        """Add one packet to the windows, the current bucket, and the sender share.

        `kind` is "rx" for a packet heard and "tx" for one the bot asked the radio to
        send. Both are summed by the same object so they can be compared, but they are
        never merged into one number.
        """
        now_wall = time.time() if at is None else float(at)
        mod = self.modulation()
        airtime_ms = lora_airtime_ms(payload_bytes, **mod)

        # The window clock is monotonic, but a sample written to a bucket is placed on
        # the wall clock, so the two must be tied at record time.
        self._samples.append((time.monotonic(), airtime_ms, kind))
        self._add_bucket(now_wall, airtime_ms, kind, payload_bytes)
        if sender:
            key = str(sender).strip().lower()
            if key:
                pending = self._senders.setdefault(key, [0.0, 0])
                pending[0] += airtime_ms
                pending[1] += 1

        return airtime_ms

    def _bucket_ts(self, now_wall: float) -> int:
        return int(now_wall // self.sample_seconds) * self.sample_seconds

    def _add_bucket(self, now_wall: float, airtime_ms: float, kind: str, payload_bytes: int) -> None:
        ts = self._bucket_ts(now_wall)
        if not self._bucket or self._bucket["bucket_ts"] != ts:
            # Only the current bucket is kept: the flush writes it, and a long-running
            # process must not hold every bucket it has ever seen.  The bucket is not
            # cleared while it is still the current one, so a flush inside a bucket
            # writes the whole bucket rather than the part since the last flush.
            self._bucket = {
                "bucket_ts": ts,
                "rx_ms": 0.0,
                "tx_ms": 0.0,
                "packets": 0,
                "rx_bytes": 0,
            }

        if kind == "tx":
            self._bucket["tx_ms"] += airtime_ms
        else:
            self._bucket["rx_ms"] += airtime_ms
            self._bucket["rx_bytes"] += max(0, int(payload_bytes))
        self._bucket["packets"] += 1

    # ------------------------------------------------------------------
    # Windows
    # ------------------------------------------------------------------

    def window_airtime(self, seconds: float) -> dict[str, float]:
        """Sum of the samples inside a rolling window, split by kind."""
        now = time.monotonic()
        rx = 0.0
        tx = 0.0
        kept = 0
        for seen, airtime_ms, kind in self._samples:
            if now - seen > seconds:
                continue
            kept += 1
            if kind == "tx":
                tx += airtime_ms
            else:
                rx += airtime_ms
        return {"rx_ms": rx, "tx_ms": tx, "samples": kept}

    def ewma_airtime(self, half_life: Optional[float] = None) -> dict[str, float]:
        """Decayed sum, so a window reads as a level rather than as a spike.

        A hard window answers "how much in the last 60 seconds"; the decayed sum
        answers "what level is the channel at", which is what a chart line needs not to
        jump on a single burst.
        """
        hl = float(half_life or self.half_life_seconds)
        if hl <= 0:
            return {"rx_ms": 0.0, "tx_ms": 0.0, "samples": 0}

        now = time.monotonic()
        rx = 0.0
        tx = 0.0
        for seen, airtime_ms, kind in self._samples:
            weight = 0.5 ** ((now - seen) / hl)
            if kind == "tx":
                tx += airtime_ms * weight
            else:
                rx += airtime_ms * weight
        return {"rx_ms": rx, "tx_ms": tx, "samples": len(self._samples)}

    def windows(self) -> dict[str, dict[str, Any]]:
        """Rolling windows as percentages of their own duration."""
        result: dict[str, dict[str, Any]] = {}
        for name, seconds in self.window_seconds.items():
            totals = self.window_airtime(seconds)
            duration_ms = seconds * 1000.0
            result[name] = {
                "seconds": seconds,
                "rx_ms": round(totals["rx_ms"], 2),
                "tx_ms": round(totals["tx_ms"], 2),
                "rx_percent": round(totals["rx_ms"] / duration_ms * 100.0, 2) if duration_ms else 0.0,
                "tx_percent": round(totals["tx_ms"] / duration_ms * 100.0, 2) if duration_ms else 0.0,
                "samples": totals["samples"],
            }
        return result

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------

    def fields(self) -> dict[str, Any]:
        """Current state as a flat dict, for JSON and for key=value output.

        One key per window rather than a nested object: a monitoring agent reading the
        key=value form takes one field per line, and a nested payload is a second
        format to keep in step for no benefit.
        """
        mod = self.modulation()
        ewma = self.ewma_airtime()
        fields: dict[str, Any] = {
            "enabled": self.enabled,
            "sf": mod["sf"],
            "bw_khz": mod["bw_khz"],
            "coding_rate": mod["cr_den"],
            "preamble": mod["preamble"],
            "samples": len(self._samples),
        }
        for name in WINDOW_NAMES:
            seconds = self.window_seconds[name]
            totals = self.window_airtime(seconds)
            duration_ms = seconds * 1000.0
            fields[f"{name}_seconds"] = seconds
            fields[f"{name}_rx_ms"] = round(totals["rx_ms"], 2)
            fields[f"{name}_tx_ms"] = round(totals["tx_ms"], 2)
            fields[f"{name}_rx_percent"] = round(totals["rx_ms"] / duration_ms * 100.0, 2) if duration_ms else 0.0
            fields[f"{name}_tx_percent"] = round(totals["tx_ms"] / duration_ms * 100.0, 2) if duration_ms else 0.0
        fields["half_life_seconds"] = self.half_life_seconds
        fields["ewma_rx_ms"] = round(ewma["rx_ms"], 2)
        fields["ewma_tx_ms"] = round(ewma["tx_ms"], 2)
        return fields

    def flush(self, conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
        """Write the current bucket, the sender increments, and the live snapshot.

        Called at the writer's own cadence, once per batch, so no extra transaction is
        opened on a hot path.  A bucket is written only when it has samples: an empty
        bucket must stay absent so the reader can tell "nothing seen" from "nothing
        written".  The conflict clause uses max() rather than an assignment because the
        same bucket can be flushed more than once while it is still the current one.
        """
        if not self.enabled or not self._bucket:
            return None

        row = self._bucket
        conn.execute(
            """
            INSERT INTO occupancy_samples (bucket_ts, sample_seconds, rx_ms, tx_ms, packets, rx_bytes)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(bucket_ts, sample_seconds) DO UPDATE SET
                rx_ms = max(rx_ms, excluded.rx_ms),
                tx_ms = max(tx_ms, excluded.tx_ms),
                packets = max(packets, excluded.packets),
                rx_bytes = max(rx_bytes, excluded.rx_bytes)
            """,
            (
                row["bucket_ts"],
                self.sample_seconds,
                row["rx_ms"],
                row["tx_ms"],
                row["packets"],
                row["rx_bytes"],
            ),
        )

        now_wall = time.time()
        for sender, (airtime_ms, packets) in self._senders.items():
            conn.execute(
                """
                INSERT INTO occupancy_senders (sender, airtime_ms, packets, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(sender) DO UPDATE SET
                    airtime_ms = airtime_ms + excluded.airtime_ms,
                    packets = packets + excluded.packets,
                    last_seen = excluded.last_seen
                """,
                (sender, airtime_ms, packets, now_wall, now_wall),
            )
        self._senders.clear()

        conn.execute(
            """
            INSERT INTO bot_metadata (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (LIVE_METADATA_KEY, json.dumps(self.fields()), now_wall),
        )

        conn.commit()
        return row

    def history(
        self,
        conn: sqlite3.Connection,
        start: Optional[float] = None,
        end: Optional[float] = None,
    ) -> dict[str, Any]:
        """Buckets in a window, as sums and counts.

        Buckets that were never written are absent from the result: the caller renders
        them as a gap. A bucket written with zero airtime is a real zero, which is a
        different fact and must not be collapsed into the gap.
        """
        moment = time.time()
        end_ts = moment if end is None else float(end)
        start_ts = end_ts - 24 * 3600 if start is None else float(start)
        if end_ts < start_ts:
            start_ts, end_ts = end_ts, start_ts

        rows = conn.execute(
            """
            SELECT bucket_ts, sample_seconds, rx_ms, tx_ms, packets, rx_bytes
            FROM occupancy_samples
            WHERE bucket_ts >= ? AND bucket_ts <= ?
            ORDER BY bucket_ts ASC
            """,
            (start_ts, end_ts),
        ).fetchall()

        buckets = [dict(row) for row in rows]
        return {
            "start_time": int(start_ts),
            "end_time": int(end_ts),
            "sample_seconds": self.sample_seconds,
            "buckets": buckets,
            "total_rx_ms": round(sum(b["rx_ms"] for b in buckets), 2),
            "total_tx_ms": round(sum(b["tx_ms"] for b in buckets), 2),
            "packets": sum(b["packets"] for b in buckets),
            **{
                f"p{int(q * 100)}_rx_ms": nearest_rank_percentile([b["rx_ms"] for b in buckets], q)
                for q in self.quantiles
            },
        }

    def top_senders(self, conn: sqlite3.Connection, limit: int = 10) -> list[dict[str, Any]]:
        """Senders ranked by airtime, not by packet count.

        A talker that sends many small packets can cost more airtime than one that sends
        a long message, so ranking on packets answers a different question.
        """
        limit = max(1, min(int(limit), 50))
        rows = conn.execute(
            """
            SELECT sender, airtime_ms, packets, first_seen, last_seen
            FROM occupancy_senders
            ORDER BY airtime_ms DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def read_live(self, conn: sqlite3.Connection) -> dict[str, Any]:
        """Read the snapshot the writer left, with its age.

        The age is part of the answer: a window that has not been refreshed is not a
        measurement, and a reader that does not carry the age cannot tell a quiet
        channel from a bot that stopped recording.
        """
        row = conn.execute(
            "SELECT value, updated_at FROM bot_metadata WHERE key = ?", (LIVE_METADATA_KEY,)
        ).fetchone()
        if not row:
            return {"stale": True, "age_seconds": None, "fields": {}}

        try:
            fields = json.loads(row[0])
        except (TypeError, ValueError):
            return {"stale": True, "age_seconds": None, "fields": {}}

        updated = float(row[1] or 0.0)
        age = time.time() - updated
        return {
            "stale": age > max(self.sample_seconds * 2.0, 120.0),
            "age_seconds": round(age, 1),
            "fields": fields,
        }
