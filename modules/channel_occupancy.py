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

On top of the airtime sums, each window also carries a **network view** of what
this node heard: distinct senders, unique packet hashes against heard packets (the
repetition factor the flood router leaves behind), and the average hop depth of
heard packets. These are counts of *who and what* was on air, not more airtime.

They are deliberately **not written to the buckets**. The buckets store sums and
counts because those re-aggregate correctly across windows; cardinalities (distinct
senders, unique hashes) do not — the same talker in two buckets is one talker, and
a reader summing the columns would over-count exactly the busy senders the numbers
exist to rank. The network view therefore lives in the ring buffer and in the live
snapshot, and its retention is the ring buffer's (`max_samples`), not the bucket
retention. And it stays a *view*: hashes cover only what this node heard, so the
repetition factor is a floor on the flooding, not a census of it.
"""

from __future__ import annotations

import configparser
import json
import logging
import math
import re
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

# The hash MeshCore reports when it could not hash a packet (all-zero, 16 hex
# chars). `rf_log` skips it for its own repeat tracking; counting it as a packet
# identity would merge every unhashable packet into one "unique" message and
# report the repetition factor of a noisy channel as calm.
INVALID_PACKET_HASH = "0000000000000000"


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


# Reserved sender key for heard airtime this radio cannot trace to a sender: the
# packet types that name no origin on the wire, of which message traffic is the
# common one — a flood packet carries its route, not its sender. It is a bucket, not
# a station — the senders table stays complete because of it, and it is excluded from
# the talker count because counting an absence of identity as one talker would be a
# made-up station.
UNATTRIBUTED_SENDER = "unattributed"

# MeshCore's flood path appends one entry per relay that repeats a packet, and the
# entry is that relay's node address: the first bytes of its public key, 2 or 3
# bytes depending on the packet's transport size. The last entry is therefore the
# station that put this copy on the air, and the depth difference between copies of
# one tree is that same per-hop width — the floor model prices the shallower copies
# with it.
_PATH_BYTES_PER_HOP = 2

# How many packet identities the hash→author cache holds. The window a note has
# to land inside is one flush: unattributed increments live in memory until the
# writer's next batch, so the cache only needs the hashes heard since then, plus
# margin for a burst. Beyond the margin the oldest entries leave first, and an
# evicted hash only means what it has always meant: that copy stays unattributed.
AUTHOR_CACHE_CAPACITY = 4096

# How long a tracked unattributed increment waits in memory for the radio's
# proof of authorship before it is written. The RF copy is recorded on the
# packet event, but the decrypted message that names the author can arrive
# seconds later — writing on every packet-stream drain would always lose that
# race.  Past the grace the increment is written as it was heard, unattributed,
# and its window closes: a flushed day row is history.
ATTRIBUTION_GRACE_SEC = 60.0


def emitter_label(
    path_chunks: Optional[list[str]],
    sender_key: Optional[str],
    path_len: Optional[int],
) -> str:
    """The station that emitted the copy this radio heard, as an honest label.

    MeshCore flood routing names every relay on the wire: the path is the chain of
    node addresses — the first bytes of each relay's public key — and the LAST entry
    is the station whose transmission this copy physically is. That is the emitter,
    and for channel occupancy it is the right subject: the emitter pays for the
    copy, whoever wrote the payload inside it.

    A copy with no path came from the sender's own antenna, so the sender IS the
    emitter — but only when the sender is a full key; an undecryptable direct packet
    names no one. A path entry is a prefix, and a prefix is kept as a prefix: this
    function never resolves identities, the reader does, and only when the prefix
    belongs to exactly one known contact. With no path and no full sender key the
    copy goes to the unattributed bucket — airtime counted, identity admitted as
    unknown, never invented.
    """
    if path_chunks:
        last = str(path_chunks[-1]).strip().lower()
        if re.fullmatch(r"[0-9a-f]{4,6}", last):
            return last
    if (path_len == 0 or not path_chunks) and sender_key:
        key = str(sender_key).strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", key):
            return key
    return UNATTRIBUTED_SENDER


def entry_relay_label(path_chunks: Optional[list[str]]) -> Optional[str]:
    """Where a relayed copy entered the mesh, as an honest label, or None.

    MeshCore's flood path lists relays, never the station that wrote the payload:
    a multicast's routing header carries the channel's transport code and one node
    address per relay, so the FIRST entry is the first relay that repeated the
    packet — as far as this radio can prove, where the copy entered the mesh. It
    is not the author (that address is nowhere on the wire for a multicast), and
    it is not the emitter (that is the last entry, `emitter_label`'s subject).

    A copy heard directly has no relay at all, so its answer is None: charging
    its airtime to an entry relay would invent a hop the wire did not carry, and
    a direct copy's station is already accounted by the emitter ledger. A TRACE
    path's per-hop SNR samples are not node addresses and match nothing here.
    """
    if not path_chunks:
        return None
    first = str(path_chunks[0]).strip().lower()
    if re.fullmatch(r"[0-9a-f]{4,6}", first):
        return first
    return None


def normalize_packet_hash(value: Any) -> Optional[str]:
    """Return a usable packet hash, or None when the value carries no identity.

    The all-zero hash is MeshCore's "could not hash this packet" marker, and an
    empty or missing value is the same fact. None keeps them out of the unique
    count rather than collapsing them into one identity shared by unrelated
    packets, which would read as a calm x1 when the channel was full of
    unhashable traffic.
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text or text == INVALID_PACKET_HASH:
        return None
    return text


def normalize_hops(value: Any) -> Optional[int]:
    """Return a hop count, or None when the value is missing or impossible.

    0 is a real answer (heard direct), None means the packet carried no path
    information at all. Averaging 0 in for an unknown path would understate hop
    depth — transit traffic read as local traffic, which is the opposite of what
    the figure is looked at for.
    """
    try:
        hops = int(value)
    except (TypeError, ValueError):
        return None
    if hops < 0 or hops > 255:
        return None
    return hops


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
        # How long `flush` holds a tracked unattributed increment so a late
        # decryption can still claim it (see `note_author`).
        self.attribution_grace_sec = self._float("attribution_grace_sec", ATTRIBUTION_GRACE_SEC)

        # Modulation. The device values win over the configured ones when the device
        # reports them, because a bot configured for SF9 on a mesh running SF11
        # reports an airtime that no packet on the channel has ever had.
        self.fallback_sf = self._int("spreading_factor", DEFAULT_SF)
        self.fallback_bw_khz = normalize_bw_khz(self._float("bandwidth_khz", DEFAULT_BW_KHZ))
        self.fallback_cr_den = normalize_cr_den(self._int("coding_rate", DEFAULT_CR_DEN))
        self.fallback_preamble = self._int("preamble_symbols", DEFAULT_PREAMBLE)
        self.use_device_modulation = self._bool("use_device_modulation", True)
        self.device_modulation: dict[str, Any] = {}

        # Ring buffer of samples: (monotonic, airtime_ms, kind, sender_key,
        # packet_hash, hops). Bounded, otherwise a busy mesh grows it for the
        # whole process lifetime.
        # (monotonic stamp, airtime ms, kind, sender key, packet hash, hops, payload
        #  bytes, path bytes-per-hop, emitter label)
        self._samples: deque[tuple[float, float, str, Optional[str], Optional[str], Optional[int], int, Optional[int], Optional[str]]] = deque(
            maxlen=self.max_samples
        )
        self._bucket: dict[str, Any] = {}
        # Pending per-sender increments since the last flush, keyed by
        # (sender, UTC day): airtime_ms, packets and the advert slice of that
        # airtime.  Day-keyed because the page shows each station over the
        # period its selector picks, and a cumulative counter cannot be cut
        # back to a window — the day rows are the same facts at a finer grain,
        # and the per-station totals are aggregated from them at flush time.
        # Kept as increments rather than totals because the flush writes deltas,
        # and writing a total on top of a stored total would double-count.
        self._senders: dict[tuple[str, int], list[float]] = {}
        # Same increments keyed by EMITTER: the station that put the heard copy on
        # the air, which MeshCore names in the flood path.  A separate ledger from
        # the senders' one on purpose — one copy has a payload carrier and an
        # emitter, two different facts, and the two tables must never be summed.
        self._emitters: dict[tuple[str, int], list[float]] = {}
        # Same increments keyed by ENTRY RELAY: the first relay MeshCore's flood
        # path names, i.e. where a relayed copy entered the mesh as far as this
        # radio can prove.  A third separate ledger on purpose — one copy has a
        # payload carrier, an emitter and an entry point, three different facts,
        # and the three tables must never be summed. A direct copy belongs to
        # none of it: no relay entered anything.
        self._relays: dict[tuple[str, int], list[float]] = {}
        # hash → author, learned from this radio's own decryption of a message
        # event (see `note_author`). Bounded: a busy mesh sees thousands of
        # identities, and an entry earns its keep only until the next flush.
        self._authors: dict[str, str] = {}
        # The unattributed increments still pending, kept per packet identity so
        # `note_author` can move them to the author before they are ever written:
        # airtime_ms, packets, advert_ms and the wall time of the newest copy
        # heard under that identity, which is what `flush` measures the
        # attribution grace against.  Only pending increments are convertible:
        # a flushed day row is history.
        self._unattr_by_hash: dict[tuple[int, str], list[float]] = {}

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
        packet_hash: Optional[str] = None,
        hops: Optional[int] = None,
        advert: bool = False,
        emitter: Optional[str] = None,
        entry_relay: Optional[str] = None,
        path_bytes_per_hop: Optional[int] = None,
        at: Optional[float] = None,
    ) -> float:
        """Add one packet to the windows, the current bucket, and the sender share.

        `kind` is "rx" for a packet heard and "tx" for one the bot asked the radio to
        send. Both are summed by the same object so they can be compared, but they are
        never merged into one number.

        `advert` marks the payload as an ADVERT, the maintenance traffic every
        MeshCore node broadcasts. It only rides along into the sender and emitter
        shares, where the advert slice of each station's airtime is kept apart from
        the rest — adverts are what a station costs for being present, not for
        talking.

        `emitter` is the station whose antenna put THIS copy on the air, as an
        `emitter_label` string: full key for a direct copy, the flood path's last
        node address for a relayed one, the unattributed bucket when neither is
        known. A transmission is always its own emitter, so a tx sample without an
        explicit emitter is charged to the sender.

        `path_bytes_per_hop` is the width one relay adds to the path on this packet
        (2 or 3 bytes in MeshCore); the floor prices the shallower copies of a tree
        with it instead of assuming the width.

        `entry_relay` is where a relayed copy entered the mesh, as an
        `entry_relay_label` string: the flood path's FIRST node address, the first
        relay that repeated the packet. Only relayed copies have one — a copy
        heard directly entered nothing, and charging it to a relay would invent a
        hop the wire did not carry. It is neither the author (a multicast names
        its writer nowhere on the wire) nor the emitter (the last entry); it is
        the gateway, a third fact kept in its own ledger for the same reason the
        other two are kept apart.

        `packet_hash` and `hops` feed the network view and are optional: a sample
        without them still costs its airtime, it just carries no identity (hash) or
        no route (hops) for the window counts. They are normalized here so every
        caller gets the same "unknown is not zero" rule. Every heard copy is one
        sample, flood repetitions included: each repetition occupies the single radio
        channel, so each one pays its airtime.
        """
        now_wall = time.time() if at is None else float(at)
        mod = self.modulation()
        airtime_ms = lora_airtime_ms(payload_bytes, **mod)
        # The day the sample belongs to is the wall-clock UTC day, so a window
        # query can sum whole days and a day boundary inside a batch does not
        # blur two days into one row.
        day_ts = int(now_wall) // 86400 * 86400

        sender_key: Optional[str] = None
        packet_id = normalize_packet_hash(packet_hash)
        if sender:
            key = str(sender).strip().lower()
            if key:
                sender_key = key
        if sender_key in (None, UNATTRIBUTED_SENDER) and packet_id:
            # A copy this radio could not name may still have an author the radio
            # itself learned by decrypting the message — `note_author` keeps that
            # proof by packet identity, so an identical copy heard again is
            # attributed from the start rather than sent to the unknown bucket.
            # A copy the wire already named keeps that name: the senders ledger
            # follows the wire, and a later note does not overwrite a carrier.
            sender_key = self._authors.get(packet_id) or sender_key
        if sender_key:
            pending = self._senders.setdefault((sender_key, day_ts), [0.0, 0, 0.0])
            pending[0] += airtime_ms
            pending[1] += 1
            if advert:
                pending[2] += airtime_ms
            if sender_key == UNATTRIBUTED_SENDER and packet_id:
                # Track the increment by identity only when it actually landed in
                # the unknown bucket, so a note arriving before the flush can move
                # exactly that. A copy the author cache already named is not in
                # the bucket, and tracking it would let a later note move airtime
                # the bucket never carried.
                tracked = self._unattr_by_hash.setdefault((day_ts, packet_id), [0.0, 0, 0.0, 0.0])
                tracked[0] += airtime_ms
                tracked[1] += 1
                if advert:
                    tracked[2] += airtime_ms
                # The newest copy heard under this identity is what `flush`
                # measures the attribution grace against.
                tracked[3] = float(now_wall)

        emitter_key = str(emitter).strip().lower() if emitter else None
        if emitter_key is None and kind == "tx" and sender_key:
            emitter_key = sender_key
        if emitter_key:
            pending = self._emitters.setdefault((emitter_key, day_ts), [0.0, 0, 0.0])
            pending[0] += airtime_ms
            pending[1] += 1
            if advert:
                pending[2] += airtime_ms

        relay_key = str(entry_relay).strip().lower() if entry_relay else None
        if relay_key and re.fullmatch(r"[0-9a-f]{4,6}", relay_key):
            # A node address only: a full key never arrives here (the flood path
            # carries prefixes), and anything that is not 2 or 3 bytes of hex is
            # not a relay this wire proved.
            pending = self._relays.setdefault((relay_key, day_ts), [0.0, 0, 0.0])
            pending[0] += airtime_ms
            pending[1] += 1
            if advert:
                pending[2] += airtime_ms

        # The window clock is monotonic, but a sample written to a bucket is placed on
        # the wall clock, so the two must be tied at record time. The payload and the
        # hop count ride along so the channel floor can price the copies of a flood
        # tree that never reached this antenna.
        self._samples.append(
            (
                time.monotonic(),
                airtime_ms,
                kind,
                sender_key,
                normalize_packet_hash(packet_hash),
                normalize_hops(hops),
                max(0, int(payload_bytes)),
                path_bytes_per_hop,
                emitter_key,
            )
        )
        self._add_bucket(now_wall, airtime_ms, kind, payload_bytes)

        return airtime_ms

    def note_author(self, packet_hash: Any, author: Any) -> bool:
        """Attach a packet identity to the author this radio's decryption named.

        The radio decrypting a message and naming its author is the same class of
        proof as an advert's signature — the network authenticated it, not the bot
        guessed it — so the copies of that packet this station heard, and whose
        increment is still pending in memory, move out of the unattributed bucket
        before anything is written.  `flush` holds a tracked increment in memory
        for `attribution_grace_sec` seconds past the newest copy it heard under
        that identity, because the decryption naming the author reaches this
        listener after the RF copy; past that grace the increment is written as
        it was heard, unattributed, and its window closes.  A copy already
        flushed is history: its row keeps saying unattributed, because
        relabeling written rows would make the ledger claim a knowledge this
        station did not have at the time.

        Only a full 64-hex key can be an author; a short prefix is not an identity.
        Returns False when the note is not usable, True otherwise — noting an
        already-noted hash again is harmless and costs nothing.  The emitter
        ledger is deliberately untouched: an author is who wrote the payload, an
        emitter is whose antenna carried the copy, and those facts are never
        merged.
        """
        packet_id = normalize_packet_hash(packet_hash)
        author_key = str(author or "").strip().lower()
        if not packet_id or not re.fullmatch(r"[0-9a-f]{64}", author_key):
            return False
        if packet_id not in self._authors and len(self._authors) >= AUTHOR_CACHE_CAPACITY:
            # dict keeps insertion order, so the first key is the oldest note.
            self._authors.pop(next(iter(self._authors)), None)
        self._authors[packet_id] = author_key

        moved_packets = 0
        for (day_ts, hashed), increment in list(self._unattr_by_hash.items()):
            if hashed != packet_id:
                continue
            self._unattr_by_hash.pop((day_ts, hashed))
            airtime_ms, packets, advert_ms, _heard_at = increment
            left = self._senders.get((UNATTRIBUTED_SENDER, day_ts))
            if left is not None:
                left[0] -= airtime_ms
                left[1] -= packets
                left[2] -= advert_ms
                if not any(left):
                    # Nothing is left in the bucket for that day: drop the row
                    # rather than writing an empty one that renders as a station
                    # with zero airtime.
                    self._senders.pop((UNATTRIBUTED_SENDER, day_ts), None)
            target = self._senders.setdefault((author_key, day_ts), [0.0, 0, 0.0])
            target[0] += airtime_ms
            target[1] += packets
            target[2] += advert_ms
            moved_packets += int(packets)
        if moved_packets:
            self.logger.debug(
                "occupancy: attributed %d pending copy(ies) of packet %s to %s",
                moved_packets,
                packet_id[:16],
                author_key[:16],
            )
        return True

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

    def window_airtime(self, seconds: float) -> dict[str, Any]:
        """Sum of the samples inside a rolling window, split by kind, plus network counts.

        The airtime sums cover both kinds, as always. The network counts (senders,
        hashes, hops) cover rx samples only: they answer "who and what was on air
        around this node", and the bot's own transmissions are not talkers on that
        network — the tx share already answers the bot's own cost, apart and unmerged.

        ``hash_known`` counts the heard packets that carried a usable hash; the
        repetition factor derived from it is computed over those alone, because a
        packet with no identity cannot be matched to a repeat of itself.

        ``rx_emitter_labels`` counts the distinct emitter LABELS among the heard
        copies — the flood path's last node address, or the full key of a station
        heard directly. Labels, not stations: one relay can appear under its
        2-byte and its 3-byte address, and a prefix names a crowd as readily as
        it names a node, so this counts what the wire proved without claiming an
        identity. The unattributed bucket names nobody and is never counted.

        ``rx_floor_ms`` is the floor of the channel's total use, not of what this
        antenna heard: a copy heard deep in a flood tree proves the shallower copies
        that came before it, even where they ran in parallel out of earshot — the
        CSMA paradox, carrier sense is local while the channel is one shared medium.
        The floor adds those proven-but-unheard copies to the heard sums; it never
        removes a repetition that was heard, and copies it cannot prove (parallel
        branches of the tree) stay outside it, so the floor is a floor. A window's
        sums can therefore exceed the window's duration, and that is a fact about a
        shared channel, not an overflow to clamp.
        """
        now = time.monotonic()
        rx = 0.0
        tx = 0.0
        kept = 0
        senders: set[str] = set()
        hashes: set[str] = set()
        hash_known = 0
        hops_sum = 0
        hops_known = 0
        unattributed = 0
        per_hash: dict[str, list[tuple[Optional[int], float, int, Optional[int]]]] = {}
        unknown_hash_ms = 0.0
        emitter_labels: set[str] = set()
        for entry in self._samples:
            seen, airtime_ms, kind, sender, packet_hash, hops, payload_bytes, hop_width, emitter = entry
            if now - seen > seconds:
                continue
            kept += 1
            if kind == "tx":
                tx += airtime_ms
                continue
            rx += airtime_ms
            if sender:
                if sender == UNATTRIBUTED_SENDER:
                    unattributed += 1
                else:
                    senders.add(sender)
            if emitter and emitter != UNATTRIBUTED_SENDER:
                emitter_labels.add(emitter)
            if packet_hash:
                hash_known += 1
                hashes.add(packet_hash)
                per_hash.setdefault(packet_hash, []).append((hops, airtime_ms, payload_bytes, hop_width))
            else:
                unknown_hash_ms += airtime_ms
            if hops is not None:
                hops_sum += hops
                hops_known += 1
        return {
            "rx_ms": rx,
            "tx_ms": tx,
            "samples": kept,
            "rx_senders": len(senders),
            "rx_unattributed": unattributed,
            "rx_emitter_labels": len(emitter_labels),
            "rx_hash_known": hash_known,
            "rx_unique_packets": len(hashes),
            "rx_hops_sum": hops_sum,
            "rx_hops_known": hops_known,
            "rx_floor_ms": self._rx_floor_ms(per_hash, unknown_hash_ms),
        }

    def _rx_floor_ms(self, per_hash: dict[str, list[tuple[Optional[int], float, int, Optional[int]]]], unknown_hash_ms: float) -> float:
        """Floor of the shared channel's use over one window, in milliseconds.

        Per heard packet identity: its heard copies at their measured cost, plus one
        priced copy for each depth from the sender to the deepest copy this radio
        heard that no heard copy carries — a packet with a four-entry path was put on
        air at least four times before it reached this antenna, and the intermediate
        copies are shorter by the path the deeper ones carry, so the deepest heard
        copy bounds their cost. The per-hop width is the packet's own when it was
        recorded (MeshCore paths are 2 or 3 bytes wide) and the constant fallback
        otherwise. A copy with no hash counts for itself alone: with no identity its
        repeats cannot be matched to a tree, and pricing an unprovable tree from an
        unknown would dress absence of evidence up as measurement.
        """
        total = unknown_hash_ms
        if not per_hash:
            return total
        mod = self.modulation()
        for copies in per_hash.values():
            total += sum(airtime_ms for _, airtime_ms, _, _ in copies)
            depths = {hops for hops, _, _, _ in copies if hops is not None}
            if not depths:
                continue
            deepest = max(depths)
            deepest_copies = [entry for entry in copies if entry[0] == deepest]
            widest = max(pb for _, _, pb, _ in deepest_copies)
            widths = [w for _, _, _, w in deepest_copies if w]
            width_per_hop = widths[0] if widths else _PATH_BYTES_PER_HOP
            for depth in range(deepest):
                if depth not in depths:
                    total += lora_airtime_ms(
                        max(1, widest - width_per_hop * (deepest - depth)), **mod
                    )
        return total

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
        for seen, airtime_ms, kind, *_ in self._samples:
            weight = 0.5 ** ((now - seen) / hl)
            if kind == "tx":
                tx += airtime_ms * weight
            else:
                rx += airtime_ms * weight
        return {"rx_ms": rx, "tx_ms": tx, "samples": len(self._samples)}

    @staticmethod
    def _network_counts(totals: dict[str, Any]) -> dict[str, Any]:
        """Derive the network view of one window from its rx counts.

        ``dup_factor`` is heard-hashed-packets over unique hashes: x1.0 means every
        hashed packet was heard once, x2.5 means the flood router repeated them. It
        is None when no heard packet carried a hash — an unknown repetition factor
        rendered as 1.0 would claim a quiet network from the absence of evidence.
        ``avg_hops`` is over the heard packets whose path was readable, for the same
        reason. ``distinct_senders`` counts identities this radio could name: the
        reserved unattributed bucket is heard airtime, not a station, and it is
        reported separately in ``unattributed_packets``.
        """
        unique = totals["rx_unique_packets"]
        hash_known = totals["rx_hash_known"]
        hops_known = totals["rx_hops_known"]
        return {
            "distinct_senders": totals["rx_senders"],
            "unattributed_packets": totals["rx_unattributed"],
            "emitter_labels": totals["rx_emitter_labels"],
            "hash_known": hash_known,
            "unique_packets": unique,
            "dup_factor": round(hash_known / unique, 2) if unique else None,
            "hops_known": hops_known,
            "avg_hops": round(totals["rx_hops_sum"] / hops_known, 1) if hops_known else None,
            "rx_floor_ms": round(totals["rx_floor_ms"], 2),
        }

    def windows(self) -> dict[str, dict[str, Any]]:
        """Rolling windows as percentages of their own duration, with the network view."""
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
                # A floor over the shared channel, above 100% by design when flood
                # copies ran in parallel out of earshot of this radio.
                "rx_floor_percent": round(totals["rx_floor_ms"] / duration_ms * 100.0, 2) if duration_ms else 0.0,
                "samples": totals["samples"],
                **self._network_counts(totals),
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
            fields[f"{name}_rx_floor_percent"] = (
                round(totals["rx_floor_ms"] / duration_ms * 100.0, 2) if duration_ms else 0.0
            )
            for key, value in self._network_counts(totals).items():
                fields[f"{name}_{key}"] = value
        fields["half_life_seconds"] = self.half_life_seconds
        fields["ewma_rx_ms"] = round(ewma["rx_ms"], 2)
        fields["ewma_tx_ms"] = round(ewma["tx_ms"], 2)
        return fields

    def flush(self, conn: sqlite3.Connection, force: bool = False) -> Optional[dict[str, Any]]:
        """Write the current bucket, the sender increments, and the live snapshot.

        Called at the writer's own cadence, once per batch, so no extra transaction is
        opened on a hot path.  A bucket is written only when it has samples: an empty
        bucket must stay absent so the reader can tell "nothing seen" from "nothing
        written".  The conflict clause uses max() rather than an assignment because the
        same bucket can be flushed more than once while it is still the current one.

        The writer's cadence is faster than the radio's decryption, so a tracked
        unattributed increment is held in memory until `attribution_grace_sec`
        seconds past the newest copy heard under its packet identity: the note
        naming the author then still finds it, and the day row is written with
        the held part taken back out of the unattributed pending.  `force`
        closes that grace early, for the shutdown flush which is the last write
        this process will ever make.
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
        # Day rows first, then the per-station totals aggregated from them: the
        # two write paths cannot drift apart if one is built from the other, and
        # the day rows are what let the reader answer "this station's airtime in
        # the selected window" without pretending a cumulative counter can be cut.
        # The tracked increments still inside their attribution grace are held
        # back from this write, per day, so a late note can still claim them.
        # Every tracked increment is part of the unattributed pending it was
        # recorded into, so the day row is simply the pending with the held
        # part subtracted back out.
        held: dict[tuple[str, int], list[float]] = {}
        if not force:
            for (day_ts, _hashed), increment in self._unattr_by_hash.items():
                if now_wall - increment[3] < self.attribution_grace_sec:
                    keep = held.setdefault((UNATTRIBUTED_SENDER, day_ts), [0.0, 0, 0.0])
                    keep[0] += increment[0]
                    keep[1] += increment[1]
                    keep[2] += increment[2]
        sender_totals: dict[str, list[float]] = {}
        for (sender, day_ts), (airtime_ms, packets, advert_ms) in self._senders.items():
            if sender == UNATTRIBUTED_SENDER:
                kept = held.get((sender, day_ts))
                if kept:
                    airtime_ms = max(0.0, airtime_ms - kept[0])
                    packets = max(0, packets - int(kept[1]))
                    advert_ms = max(0.0, advert_ms - kept[2])
            if not (airtime_ms or packets or advert_ms):
                continue
            conn.execute(
                """
                INSERT INTO occupancy_sender_days (sender, day_ts, airtime_ms, packets, advert_ms, last_seen)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(sender, day_ts) DO UPDATE SET
                    airtime_ms = airtime_ms + excluded.airtime_ms,
                    packets = packets + excluded.packets,
                    advert_ms = advert_ms + excluded.advert_ms,
                    last_seen = max(last_seen, excluded.last_seen)
                """,
                (sender, day_ts, airtime_ms, packets, advert_ms, now_wall),
            )
            total = sender_totals.setdefault(sender, [0.0, 0, 0.0])
            total[0] += airtime_ms
            total[1] += packets
            total[2] += advert_ms
        # What the grace held back stays pending: the remainders become the
        # unattributed increments of the next flush, and `note_author` can
        # still move them until their window closes.
        self._senders = held
        # The windows that closed were just written as day rows.  Their
        # identities stop being convertible here on purpose: a note arriving
        # later must not reach back into rows that are already history.
        for key, increment in list(self._unattr_by_hash.items()):
            if force or now_wall - increment[3] >= self.attribution_grace_sec:
                self._unattr_by_hash.pop(key, None)
        for sender, (airtime_ms, packets, advert_ms) in sender_totals.items():
            conn.execute(
                """
                INSERT INTO occupancy_senders (sender, airtime_ms, packets, first_seen, last_seen, advert_ms)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(sender) DO UPDATE SET
                    airtime_ms = airtime_ms + excluded.airtime_ms,
                    packets = packets + excluded.packets,
                    advert_ms = COALESCE(advert_ms, 0) + excluded.advert_ms,
                    last_seen = excluded.last_seen
                """,
                (sender, airtime_ms, packets, now_wall, now_wall, advert_ms),
            )

        emitter_totals: dict[str, list[float]] = {}
        for (emitter, day_ts), (airtime_ms, packets, advert_ms) in self._emitters.items():
            conn.execute(
                """
                INSERT INTO occupancy_emitter_days (emitter, day_ts, airtime_ms, packets, advert_ms, last_seen)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(emitter, day_ts) DO UPDATE SET
                    airtime_ms = airtime_ms + excluded.airtime_ms,
                    packets = packets + excluded.packets,
                    advert_ms = advert_ms + excluded.advert_ms,
                    last_seen = max(last_seen, excluded.last_seen)
                """,
                (emitter, day_ts, airtime_ms, packets, advert_ms, now_wall),
            )
            total = emitter_totals.setdefault(emitter, [0.0, 0, 0.0])
            total[0] += airtime_ms
            total[1] += packets
            total[2] += advert_ms
        self._emitters.clear()
        for emitter, (airtime_ms, packets, advert_ms) in emitter_totals.items():
            conn.execute(
                """
                INSERT INTO occupancy_emitters (emitter, airtime_ms, packets, first_seen, last_seen, advert_ms)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(emitter) DO UPDATE SET
                    airtime_ms = airtime_ms + excluded.airtime_ms,
                    packets = packets + excluded.packets,
                    advert_ms = advert_ms + excluded.advert_ms,
                    last_seen = excluded.last_seen
                """,
                (emitter, airtime_ms, packets, now_wall, now_wall, advert_ms),
            )

        relay_totals: dict[str, list[float]] = {}
        for (relay, day_ts), (airtime_ms, packets, advert_ms) in self._relays.items():
            conn.execute(
                """
                INSERT INTO occupancy_relay_days (relay, day_ts, airtime_ms, packets, advert_ms, last_seen)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(relay, day_ts) DO UPDATE SET
                    airtime_ms = airtime_ms + excluded.airtime_ms,
                    packets = packets + excluded.packets,
                    advert_ms = advert_ms + excluded.advert_ms,
                    last_seen = max(last_seen, excluded.last_seen)
                """,
                (relay, day_ts, airtime_ms, packets, advert_ms, now_wall),
            )
            total = relay_totals.setdefault(relay, [0.0, 0, 0.0])
            total[0] += airtime_ms
            total[1] += packets
            total[2] += advert_ms
        self._relays.clear()
        for relay, (airtime_ms, packets, advert_ms) in relay_totals.items():
            conn.execute(
                """
                INSERT INTO occupancy_relays (relay, airtime_ms, packets, first_seen, last_seen, advert_ms)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(relay) DO UPDATE SET
                    airtime_ms = airtime_ms + excluded.airtime_ms,
                    packets = packets + excluded.packets,
                    advert_ms = advert_ms + excluded.advert_ms,
                    last_seen = excluded.last_seen
                """,
                (relay, airtime_ms, packets, now_wall, now_wall, advert_ms),
            )

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

        The rows sum every heard copy, flood repetitions included — each one occupied
        the channel — so the shares can exceed the wall-clock span, and the reserved
        unattributed row carries the heard airtime no identity covers. The table
        therefore adds up to the channel as this radio heard all of it, not just to
        the traffic it could name.

        ``advert_ms`` is the slice of that sender's airtime spent on ADVERT payloads —
        the presence traffic every MeshCore node broadcasts. It is NULL, not 0, for a
        sender last seen before the column existed: the split was not recorded then,
        and an unknown share must not be shown as a zero one.
        """
        limit = max(1, min(int(limit), 50))
        rows = conn.execute(
            """
            SELECT sender, airtime_ms, packets, first_seen, last_seen, advert_ms
            FROM occupancy_senders
            ORDER BY airtime_ms DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def top_emitters(self, conn: sqlite3.Connection, limit: int = 10) -> list[dict[str, Any]]:
        """Stations ranked by the airtime they physically spent on this radio's channel.

        This is the occupancy subject: the emitter of each heard copy, taken from the
        flood path's last node address (a key prefix) or from the sender when the copy
        arrived directly, and from the bot's own key for a transmission. The senders'
        table answers who WROTE the payload; this one answers who TRANSMITTED the copy
        — a relay near the antenna can top this table without ever appearing on the
        other, because relaying is what occupies the channel. The two tables hold the
        same copies twice, once per fact, and must never be summed.

        A prefix is shown as a prefix unless exactly one known contact matches it: the
        wire carries 2 to 3 leading bytes of the relay's key, and a prefix that could
        belong to several stations is not an identity this page may claim.
        """
        limit = max(1, min(int(limit), 50))
        rows = conn.execute(
            """
            SELECT emitter, airtime_ms, packets, first_seen, last_seen, advert_ms
            FROM occupancy_emitters
            ORDER BY airtime_ms DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def top_relays(self, conn: sqlite3.Connection, limit: int = 10) -> list[dict[str, Any]]:
        """Entry relays ranked by the airtime of the copies that entered the mesh through them.

        The third per-copy ledger, next to the senders' ("who wrote the payload") and
        the emitters' ("whose antenna carried this copy"): the flood path's FIRST node
        address names the first relay that repeated each heard copy — where the copy
        entered the mesh, as far as this radio can prove. It answers which gateways
        feed this node's channel, not who authored anything: a multicast names its
        writer nowhere on the wire, and the writer's own antenna is usually behind the
        first relay, not equal to it. A copy heard directly passed through no relay and
        belongs to no row here.

        Labels are node-address prefixes, 4 or 6 hex digits — never full keys, since
        only relayed copies are recorded. The reader resolves a prefix to a name only
        when exactly one known contact matches it, under the same uniqueness rule as
        the emitter labels.
        """
        limit = max(1, min(int(limit), 50))
        rows = conn.execute(
            """
            SELECT relay, airtime_ms, packets, first_seen, last_seen, advert_ms
            FROM occupancy_relays
            ORDER BY airtime_ms DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def window_cutoff_day(days: int, now: Optional[float] = None) -> int:
        """The first day row a window includes, aligned to the UTC day.

        The selector says "last 7 days" while the ledger stores whole days, so
        the oldest day overlapping the window counts whole; the caption says
        the windows are day-aligned rather than pretending to a precision the
        storage does not have.
        """
        now_wall = time.time() if now is None else float(now)
        return (int(now_wall) - max(1, int(days)) * 86400) // 86400 * 86400

    def top_senders_window(
        self, conn: sqlite3.Connection, cutoff_day: int, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Sender shares summed over day rows at or after *cutoff_day*.

        The cumulative ledger cannot be cut back to a period, so the period
        question is answered by summing the day rows — the same facts stored a
        grain finer.  A station with no day row in the window is absent, not a
        zero: it had no copy this radio recorded in it, and the day rows reach
        back only as far as the day this breakdown was introduced.
        """
        limit = max(1, min(int(limit), 50))
        rows = conn.execute(
            """
            SELECT sender,
                   SUM(airtime_ms) AS airtime_ms,
                   SUM(packets) AS packets,
                   SUM(advert_ms) AS advert_ms,
                   MAX(last_seen) AS last_seen
            FROM occupancy_sender_days
            WHERE day_ts >= ?
            GROUP BY sender
            ORDER BY airtime_ms DESC
            LIMIT ?
            """,
            (cutoff_day, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def top_emitters_window(
        self, conn: sqlite3.Connection, cutoff_day: int, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Emitter shares summed over day rows — see `top_senders_window`.

        The window is what the reader selected; the two ledgers stay separate
        for it exactly as they are cumulative-side, and never summed.
        """
        limit = max(1, min(int(limit), 50))
        rows = conn.execute(
            """
            SELECT emitter,
                   SUM(airtime_ms) AS airtime_ms,
                   SUM(packets) AS packets,
                   SUM(advert_ms) AS advert_ms,
                   MAX(last_seen) AS last_seen
            FROM occupancy_emitter_days
            WHERE day_ts >= ?
            GROUP BY emitter
            ORDER BY airtime_ms DESC
            LIMIT ?
            """,
            (cutoff_day, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def top_relays_window(
        self, conn: sqlite3.Connection, cutoff_day: int, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Entry-relay shares summed over day rows — see `top_senders_window`.

        Same day-aligned window as the other two ledgers; the three stay separate
        and are never summed, day-sliced or otherwise.
        """
        limit = max(1, min(int(limit), 50))
        rows = conn.execute(
            """
            SELECT relay,
                   SUM(airtime_ms) AS airtime_ms,
                   SUM(packets) AS packets,
                   SUM(advert_ms) AS advert_ms,
                   MAX(last_seen) AS last_seen
            FROM occupancy_relay_days
            WHERE day_ts >= ?
            GROUP BY relay
            ORDER BY airtime_ms DESC
            LIMIT ?
            """,
            (cutoff_day, limit),
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
