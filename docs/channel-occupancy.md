# Channel occupancy

This feature counts **airtime**: how much of the channel a packet takes, and how much
the bot itself takes. It is off by default.

## What it is, and what it is not

The bot is a *client* of a radio, not a controller of one. Over serial, BLE or TCP it
cannot see whether the channel was busy, so nothing here measures occupancy in the
radio-driver sense — CAD, LBT, or a clear-channel assessment. Those belong to a repeater
or a modem, and a repeater's real occupancy comes from that repeater's own API.

What this does is **compute** how long a packet of a given size occupies the channel at
a given modulation, using the Semtech AN1200.13 expression:

```
symbol_time = 2^SF / BW
airtime     = (preamble + 4.25 + symbols) * symbol_time
symbols     = 8 + ceil((8*PL - 4*SF + 28 + CRC - header) / (4*(SF - 2*DE))) * CR
```

The `4.25` is the sync pattern, which exists even with a zero-length preamble; `+16` is
the CRC bits; `-20` is the difference between implicit and explicit header mode; `CR` is
the coding-rate denominator (5..8). Low-data-rate optimization is applied by the
datasheet's own rule (symbol time ≥ 16.38 ms) unless the configuration forces it.

So a percentage from this page is a **modelled** channel load, correct for the
modulation it is given, and wrong whenever the modulation is wrong. That is why the
device's reported SF/BW/CR win over the configured ones: a bot configured for SF9 on a
mesh running SF11 reports an airtime no packet on the channel has ever had.

## rx share and tx share are never merged

| Share | What it counts | Why it cannot stand for "the channel" |
| --- | --- | --- |
| rx | airtime the packets the node **hears** would take | A node only hears part of the channel: hidden nodes, antenna gain, spatial reuse. It is a local observation. |
| tx | airtime the bot **sends** | The only part the bot can decide about. Counted at the moment the bot asks the radio to send, on the message body alone. |

A single combined percentage would present one node's hearing as the whole channel's
airtime, so the API reports them apart and nothing here adds them.

## Storage conventions

Three rules that come from the dashboard rollups and are worth knowing before reading a
chart:

- **A gap is not a zero.** A bucket that held no packet is absent from the result, not
  `0.0`. Rendering an absence as zero produces a cliff at the retention boundary that a
  reader interprets as an outage. A bucket that was observed and held nothing on air is
  a real zero and stays a zero — those are two different facts.
- **Sums and counts, never means.** A stored mean cannot be re-aggregated: averaging
  averages loses the weights. Any window is rebuilt from sums.
- **Two clocks.** Rolling windows use a monotonic clock, so an NTP correction or a
  clock-sync command cannot stretch or shrink a window. Buckets are stored as UTC epoch
  seconds, because that is what a reader queries.

Percentiles use **nearest-rank** rather than interpolation. On a handful of samples the
interpolated percentile under-reports the tail, and the tail — a burst — is the part
anyone looks at. A percentile over 60-second buckets is not the same measurement as a
percentile over the 10-second instant windows, and the API does not pretend otherwise.

## Live numbers

The windows live in the bot process. The web viewer is a separate process and can only
read what the writer left behind, so the snapshot is stored in `bot_metadata` under
`occupancy.live`, at the writer's own flush cadence, once per batch — never on the
packet path.

The snapshot's **age** is part of the answer. Without an age, a quiet channel and a bot
that stopped recording look identical. The reader marks the snapshot `stale` once it is
older than twice the bucket size (at least 120 s).

## Configuration

```ini
[Channel_Occupancy]
enabled = false
sample_seconds = 60
fast_window_seconds = 10
window_seconds = 60
slow_window_seconds = 900
half_life_seconds = 300
max_samples = 5000
use_device_modulation = true
spreading_factor = 9
bandwidth_khz = 125
coding_rate = 5
preamble_symbols = 8
```

Recording happens on the packet path that fills `packet_stream`, so it runs when the web
viewer's writer is running. Retention is the usual `[Data_Retention]` pair
(`occupancy_samples_retention_days`, `occupancy_senders_retention_days`) applied by the
hourly cleanup scheduler; pruning is what makes a "last 30 days" chart actually mean 30
days.

## Reading it

The Web Viewer page `/airtime` draws all of this — the windows, the history chart,
the percentiles and the sender share — from the two routes below, which are what it
reads. The mesh command `airtime` answers with one window and the top talkers, from the
same numbers; the page is the whole picture, the command is the short version.

`GET /api/telemetry/occupancy` — the live windows and the sender share:

```json
{
  "enabled": true,
  "age_seconds": 4.2,
  "stale": false,
  "sf": 11, "bw_khz": 125.0, "coding_rate": 8, "preamble": 8,
  "window_rx_percent": 6.4,
  "window_tx_percent": 0.7
}
```

`GET /api/telemetry/occupancy/history?days=7` — buckets as sums and counts, plus the
nearest-rank percentiles. Only buckets that were written appear in `buckets`.

Both accept `?format=text`, which renders one field per line for a monitoring agent.
Only scalar fields are rendered that way; the bucket list and the sender list are not,
so read them as JSON. Both routes are on the anonymous allowlist, like the storage
probe: the payload carries aggregate counters and sender keys that
`GET /api/contacts` already shows.

`enabled` is the setting as the reader knows it, not the value the snapshot happened to
be written under: recording can have been on when the snapshot was written and off now,
which is two facts. When they disagree the payload also carries `snapshot_enabled`, and
the page says the same thing on screen — the setting is off now, and the figures shown
were written while it was on, with the age saying how long ago. A request that fails is
not evidence about the setting either, so the page takes back its claim rather than
leaving it standing beside the error.

## What this can be used for

- Deciding whether a mesh is actually busy, or whether a channel looks busy only from
  where you sit.
- Judging a bot's own contribution — a talker sending many short packets costs more
  airtime than one sending a long message, which is why the sender share is ranked by
  airtime and not by packet count.
- Setting a fairness limit, if one is ever added. A limit has to be built on airtime,
  because a limit built on message count rewards short messages and punishes long ones.

It cannot tell you whether the channel was clear, whether a packet was collided, or
whether a repeater was hearing you. For that you need a repeater's own counters.
