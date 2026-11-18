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

Every heard copy is counted separately, flood repetitions included: on the one shared
radio channel each retransmission occupies the medium on its own. The sums can
therefore exceed the window they are measured over — an rx share of 130% is a fact
about parallel traffic, not an overflow. Each radio senses the carrier only near
itself, so two relays can put the same packet on air at the same moment out of each
other's hearing; the collision or the blend happens somewhere neither of them can
observe locally. That is the CSMA/CD paradox: carrier sense is local, the channel is
global, and a local share capped at 100% would hide the only sign of a saturated
mesh. Nothing in the pipeline clamps the ratio; the chart bar clamps at the card
width and says "more than fits" when it does.

## The channel floor

The rx share is what this antenna heard. A copy heard deep in a flood tree also
*proves* the copies that came before it: a packet received with a four-entry path was
put on air at least four times before it arrived, whether or not those earlier copies
ever reached this antenna — some of them ran in parallel elsewhere, out of earshot.
Each window therefore also carries `rx_floor_ms` and `rx_floor_percent`: the heard
copies at their measured cost, plus one priced copy for every shallower depth of a
heard tree that no heard copy carries. The shallower copies are priced from the
deepest heard one — each hop adds a path entry to the payload, a path entry being 2
or 3 bytes depending on the packet's transport size, and a copy recorded with its own
measured width is priced with that width rather than the fallback — and the
modulation model does the rest.

It is a floor, never a ceiling: parallel branches of the tree that no heard copy
proves are additional and unprovable, so they stay out. A copy with no packet hash
counts for itself alone: without an identity its repeats cannot be matched to a tree,
and pricing an unprovable tree from an unknown would dress absence of evidence up as
measurement.

## The sender share

`occupancy_senders` ranks senders by airtime, every heard copy included, and the
table adds up to the whole heard channel. Attribution is what the wire carries, not
what a guess could supply:

| Row | Identity from |
| --- | --- |
| adverts | the advertised node's full 32-byte key, which is the first bytes of the payload: MeshCore signs adverts rather than encrypting them, so an advert relayed on a transport channel still names its advertiser |
| the bot itself | its own full key, recorded at transmission time |
| `unattributed` | everything else: the packet types that name no origin on the wire, message traffic above all |

A two-byte routing prefix is never used as a sender: it belongs to several keys, and a
wrong identity in a table read as evidence is worse than an admitted unknown. The advert
rule has a size floor rather than only a hex-shape check, because the head bytes of any
readable payload are hex-shaped and a made-up key would be read as a station; the floor
is what the advert structure actually needs (key + signature + name). On live traffic
299 of the last 300 adverts carried a head key that resolves to a tracked contact. The
reserved `unattributed` row is a bucket, not a station — it carries the airtime but is
excluded from the talker count, which stays a count of identities this radio can name.
Because every copy counts, the table's total can exceed the wall-clock span, exactly
like the rx share does.

One more honest source sits behind the `unattributed` row: this radio's own decryption.
When the radio decrypts a message, the event names its author — the network authenticated
that identity, the same proof class as an advert's signature — and when the message can be
tied to a specific logged RF copy (a correlated row, never a fallback, see #80) and the
author's key prefix completes to exactly one known contact, the copies of that packet whose
airtime has not been written yet move out of the bucket, by packet hash. What has already
been flushed stays `unattributed`: relabeling written rows would make the ledger claim a
knowledge this station did not have at the time. So the bucket shrinks from the moment a
message is understood onward, and never behind it. Undecryptable traffic — what this
radio's keys do not open — stays in the bucket, named as unknown.

The writer drains faster than the radio decrypts: the RF copy is recorded on the packet
event, and the decryption that names the author can reach this listener seconds later. A
tracked increment therefore waits in memory for `attribution_grace_sec` seconds (60 by
default) past the newest copy heard under its packet identity before the flush writes it,
and the shutdown flush closes that grace early so nothing stays behind. On MeshCore open
channels the wait usually changes nothing: the channel key is shared and the delivery
event the radio exposes names no author at all, so that anonymity is structural, not a
gap in this measurement — the bucket is its honest ceiling. The grace exists for the
traffic where an identity does exist: what this radio decrypts with its own keys.

Each row also carries an **advert slice**: the part of that sender's airtime spent on
ADVERT payloads, kept apart because adverts are what a station costs for being present
on the mesh, not for talking — a repeater that floods adverts constantly racks up
airtime without being chatty. A row written before the slice existed keeps it `NULL`,
never 0: the split was not recorded for that traffic, and a 0% there would claim a
station that never advertised. Once such a row is written again the slice counts from
that write onward, so its stated share is a floor over the row's retained history.
The page renders the share as a percent, and a dash where it is unknown.

## The emitter share: who occupies the channel

`occupancy_senders` answers who WROTE the payload. `occupancy_emitters` answers the
other question, and for channel occupancy the closer one: whose antenna put THIS copy
on the air. MeshCore's flood routing answers it on the wire — every relay appends its
node address to the path, the leading bytes of the relay's public key, 2 or 3 bytes
depending on the packet's transport size — and the LAST entry is the station that
carried the copy this radio heard. On sampled live traffic every path prefix resolved
to a tracked repeater, and the last entry of a copy heard at −43 dBm resolved to a
repeater a few metres from the antenna: the nearest relay is visible as an emitter even
though it writes no payload of its own.

| Emitter label | From |
| --- | --- |
| the path's last node address (4 or 6 hex digits) | a relayed copy — the last entry is its transmitter |
| a full public key | a copy that arrived with no path, the sender's own antenna; and a transmission the bot made, charged to its own key |
| `unattributed` | copies the wire names no one for: undecoded traffic, direct copies whose only sender bytes are truncated, and TRACE paths, whose entries are per-hop SNR hashes, not node addresses |

The label stays as the wire sent it — and only a **flood** path names relays at all.
A direct packet carries no route info, and what the decoder hands over for one is its
transport codes read as path-sized chunks: the capture path therefore derives the
path-based emitter and entry-relay labels only for `FLOOD` and `TRANSPORT_FLOOD` route
types (on live traffic one direct ACK's transport code `62016C` reached the ledgers
before this gate, and its two 6-char labels under `6201` froze that node's fold). The
airtime is charged whatever the route type. The reader (`top_emitters`) turns a node address
into a contact name only when exactly one tracked contact begins with those bytes; an
ambiguous prefix stays bytes, because naming a prefix that could belong to several
stations is a coin flip presented as a fact. A transmission is its own emitter, and an
advert repeated by a relay spends the relay's presence budget too — the advert slice is
kept per emitter exactly as it is per sender.

The same relay can nonetheless stand in the ledger under two labels: MeshCore writes a
node address **2 bytes wide in short packets and 3 bytes wide in extended ones**, so
one antenna appears as `6201` on some copies and `62010a` on others. Two rows for one
relay read as two emitters, so the reader folds the short label into the longer one —
over the whole ledger, not just the page's rows: the wider partner is often the small
row (on live traffic 237 relay copies sat under `6201` while its only possible wider
address held 18, below the page the fold first saw), so the reader draws a wide pool
before folding, and a prefix whose unique wider address is still not in the fetched
rows is relabelled to it without summing an airtime that fetch never saw. It folds
but only when the longer address is the only emitter address the ledger knows under
that prefix, and the folded labels are listed in the row (`merged_prefixes`) rather
than absorbed silently. With two longer addresses heard, the 2-byte copies cannot be
told apart, and folding them into either would attribute copies to a station the wire
did not name; the short row stays alone. The fold is a view rule: the ledger keeps the
labels at the widths the wire sent them. The fold sees the whole ledger, not just the
page: the reader fetches a wide pool before folding, and when the one wider address is
known to the ledger but absent from the fetched rows entirely, the short row is
relabelled to it — the prefix can only mean that address — without summing in the
partner row's airtime, which that fetch never saw.

The two ledgers hold the same copies once per fact each — carrier and emitter are two
different stations — so they must never be summed: a sum would bill the channel twice.
Their value is precisely that they disagree: a repeater near the antenna can occupy
most of this channel without ever appearing in the senders table, because relaying
carries other stations' payloads. `occupancy_emitters.advert_ms` is NOT NULL, unlike
the senders' column: the split is known from this table's first write, so there is no
legacy row for a zero to hide an unknown behind.

## The entry-relay share: where relayed copies entered the mesh

The same flood path answers a third question per copy, read from its other end: the
**FIRST** node address is the first relay that repeated the packet — the gateway
through which the copy entered the mesh, as far as this radio can prove.
`occupancy_relays` (migration 39) keeps that fact apart from the other two.

It is important about what it is not. It is not the author: a multicast's routing
header carries the channel's transport code and one address per relay, never the
writing station's address, and the writer's own antenna usually sits behind the first
relay, not equal to it. It is not the emitter either: that is the path's LAST entry,
the antenna that carried the copy here. A one-hop path is the only case where the two
are the same node. And a copy heard **directly** passed through no relay at all, so it
belongs to no row here: `entry_relay_label()` returns None rather than inventing a hop
the wire did not carry, and its airtime is already accounted by the emitter ledger.
This table is therefore deliberately smaller than the other two, and absence from it
is not silence.

| Entry-relay label | From |
| --- | --- |
| the path's first node address (4 or 6 hex digits) | a relayed copy — the first entry is its gateway |
| *(no row)* | a copy heard directly: it entered nothing; and TRACE paths, whose entries are per-hop SNR hashes, not node addresses |

The label rules are the emitter's rules unchanged: the ledger stores the node address
at the width the wire sent it, the reader resolves it to a contact name only when
exactly one tracked contact begins with those bytes, and the 2-byte/3-byte fold applies
with the same uniqueness condition (`merged_prefixes` names what was merged).

Three ledgers, three facts about every relayed copy — writer, gateway, antenna — and
one airtime charge each. They must never be summed with one another; their value is
that they answer three different questions, and a gateway that carries everyone's
traffic can top this table without ever speaking for itself.

## Per-period shares: the day tables

`occupancy_senders`, `occupancy_emitters` and `occupancy_relays` are cumulative: each
row answers "since first seen, within retention", and nothing can cut a cumulative
total back to a window. A page whose history selector offers 24 h–90 d while its
tables show those counters invites exactly that misreading, so the same samples also
write three finer grains — `occupancy_sender_days`, `occupancy_emitter_days` and
`occupancy_relay_days`, one row per station per **UTC day** — and the flush builds
each cumulative row as the sum of the day rows it just wrote, so the two grains cannot
drift apart. Any window is then a sum over whole
days: `window_cutoff_day` aligns it to the day boundary, and the oldest day
overlapping the window counts whole, because part of a day nobody measured cannot be
priced.

The day rows start the day the breakdown starts. Traffic already inside the
cumulative tables before it cannot be distributed across days after the fact —
inventing that distribution would fabricate per-day facts nobody recorded — so a long
window understates traffic older than the breakdown, and the page says so rather than
letting the number speak. A window with no day row for a station is that station
absent from what this radio recorded in it, not a measured zero.

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

## The local network view

The airtime sums answer *how much* was on air. Each window also carries *who and what*
this node heard:

| Figure | How it is counted | What it cannot claim |
| --- | --- | --- |
| talkers | distinct rx senders inside the window whose packets carried an identity | Who was talking but not heard from here; and the unattributed bucket, which is heard airtime, not a station. |
| unnamed copies | rx copies inside the window charged to the reserved unattributed sender bucket | A station. It is heard airtime without a name, stated so the packet total reconciles with the talkers count. |
| emitter addresses | distinct emitter LABELS of the rx copies heard inside the window — the flood path's last node address, a full key for a direct hear | Stations: one relay can stand under its 2-byte and its 3-byte address, and a prefix names a crowd as readily as a node. The unattributed bucket adds no label. |
| repeats | hashed packets heard ÷ unique packet hashes | The full flood. Hashes cover only what this node heard, so the factor is a **floor** on the repetitions, not a census. |
| copy cost | of the hashed rx airtime (`rx_hashed_ms`), the slice spent on copies after the first one this station heard for that identity (`rx_copy_ms`) — each copy priced at its own modulation | The network's first emission. The baseline is this station's hearing, bounded by `copy_horizon_seconds`: "first" means first heard inside a horizon, so a packet re-flooded more than a horizon apart counts as a new first occurrence. The two baselines differ by a bounded, per-identity offset, never by an invented zero. And it covers hashed airtime only: an unhashable packet cannot be matched to a copy of itself, so it sits outside the measure. |
| avg hops | mean `path_length` of the heard packets whose path was readable | Transit vs local for packets whose path did not decode. |
| channel floor | heard copies plus the shallower copies each heard tree proves (see above) | Parallel branches no heard copy proves. A floor, never a ceiling. |

The mesh reply states the first five as one line, in the reader's language, and each
clause agrees with its own number: `1 talker, 1 unnamed copy, 1 emitter address` /
`1 expéditeur identifié, 1 copie sans identité, 1 adresse émettrice`. The fr clause says
*expéditeurs identifiés* and not *émetteurs* on purpose: in this feature *émetteur* names
the station whose antenna carried a copy, which is a different fact from who wrote the
payload — that label is what made a correct count read as a broken measurement when it sat
next to a larger packet total. Three counts, three questions, never added together, and a
count the window does not hold is left out rather than shown as 0.

The repetition factor is what a flood router leaves behind: the same packet heard over
several paths arrives several times with the same hash, so a window reading x2.5 was
hearing the network repeat itself. The hop depth says how far the heard traffic came
from — everything at 0 is a purely local pocket, a mean of 2 is traffic crossing the
mesh.

An unknown stays unknown, same rule as the airtime: a window that heard no hashable
packet carries *no* repetition factor rather than x1.0 (that would claim a network
without repeats from the absence of evidence), and a packet without path information is
left out of the hop mean rather than averaged in as 0 hops. The copy share follows the
same rule: `copy_percent` divides by the hashed airtime only — unhashable packets sit
outside it — and is *absent*, not 0%, when no hashed packet was heard. A share of 0% is
itself an answer: this station heard identities and none of them repeated.

Two windows of the same length are also compared: each window carries `previous` —
the equal-length window immediately before it, read from the same ring buffer — and a
`trend` over it, but only while the buffer can cover both. A fresh start has no
previous window, and a previous window with no samples is absent evidence, not a quiet
baseline: both answer `insufficient_history` rather than turning a boot into a flood.
And each window says what the buffer can prove about it: `truncated` is true when
hashed traffic was heard but the ring buffer no longer reaches back over the whole
window — the repeats and the copy cost are then bounds, and the mesh reply and the page
mark them as such (`≥`, `(bounded)`) instead of dropping them.

The copy cost is classified when the sample is recorded, while the writer's identity
memory is warm, and rides along in the sample and the bucket as precomputed sums.
Unlike the cardinalities, those sums re-aggregate: a reader adding buckets gets a
correct sum of sums. That is why `rx_hashed_ms` and `rx_copy_ms` are the one part of
the network view that **is** written to the buckets. Buckets written before the
columns existed carry NULL, and a history period that spans them reports the copy share
as unknown rather than as a ratio over part of the period — an invented average is as
dishonest as an invented zero.

These counts are deliberately **not stored in the buckets** — with the one exception
of the copy cost, whose two sums do re-aggregate (see above). The buckets hold sums and
counts because those re-aggregate across windows; cardinalities do not — the same talker
in two buckets is one talker, and a reader summing the column would over-count exactly
the busy senders the figure exists to rank. The network view therefore lives in the
ring buffer and in the live snapshot, and its retention is the ring buffer's
(`max_samples`), not the bucket retention.

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
copy_horizon_seconds = 3600
attribution_grace_sec = 60
use_device_modulation = true
spreading_factor = 9
bandwidth_khz = 125
coding_rate = 5
preamble_symbols = 8
```

Recording happens on the packet path that fills `packet_stream`, so it runs when the web
viewer's writer is running. Retention is the usual `[Data_Retention]` keys
(`occupancy_samples_retention_days`, `occupancy_senders_retention_days`,
`occupancy_emitters_retention_days`, `occupancy_relays_retention_days`) applied by the
hourly cleanup scheduler; pruning is what makes a "last 30 days" chart actually mean 30
days. The three per-station ledgers age on the same rule so a reader comparing them is
never handed a young table against an old one, and the day tables age on the same keys
as their ledgers — a selected window cannot reach further back than the totals it
describes.

`copy_horizon_seconds` is the copy-cost baseline: an identity stays "already heard"
that long, so copies of it inside the horizon are charged to the flood and a re-flood
after it counts as a new first occurrence. It bounds both the writer's identity memory
and the error the baseline can make — two occurrences of one identity more than a
horizon apart are charged as two first copies, a constant offset per identity, never an
invented zero.

## Reading it

The Web Viewer page `/airtime` draws all of this — the windows, the history chart,
the percentiles, the sender share, the emitter share and the entry-relay share — from
the two routes below, which are what it reads. The mesh command `airtime` answers with one window, the network view and this
radio's own transmitted airtime over the last 7 rolling days, from the same numbers;
the page is the whole picture, the command is the short version. The command names only
this radio because it is the only sender the bot has complete knowledge of: heard
adverts carry their advertiser's key and reach the senders table, but message traffic
names no sender on the wire, so the per-sender view of the channel belongs to the page,
not to the reply. The command reply is telegraphic and built to fit one channel body,
counted in **bytes** because an accented reply overflows the wire before it overflows a
character count. The budget is what the sender's own reply path reports: 155 minus the
radio's name and `": "`, and a further 10 when the reply leaves on a regional flood
scope — a regional radio answers from a smaller body than the same radio answering
globally. When the body runs out of room the stations clause goes first,
then the reply is split at line boundaries — the network line is part of the answer, so
it is never dropped quietly, and a sender line that is dropped is announced as dropped
rather than cut mid-way.

`GET /api/telemetry/occupancy` — the live windows, the network view and the sender share:

```json
{
  "enabled": true,
  "age_seconds": 4.2,
  "stale": false,
  "sf": 11, "bw_khz": 125.0, "coding_rate": 8, "preamble": 8,
  "window_rx_percent": 6.4,
  "window_tx_percent": 0.7,
  "window_rx_floor_percent": 11.1,
  "window_distinct_senders": 3,
  "window_unattributed_packets": 12,
  "window_emitter_labels": 2,
  "window_dup_factor": 2.4,
  "window_avg_hops": 1.5,
  "window_copy_percent": 41.3,
  "window_truncated": false,
  "window_prev_rx_ms": 1980.4
}
```

`window_dup_factor` and `window_avg_hops` are absent evidence rather than zero when no
heard packet carried a hash or a path; in the `text` format they render as an empty
value, not as `0`. `window_copy_percent` follows the same rule — `null` when no hashed
packet was heard — and `window_truncated` says when the repeats and the copy share are
bounds rather than a census. `window_prev_rx_ms` is the rx airtime of the equal window
one length back, `null` when the ring buffer does not reach that far: a monitoring
agent comparing snapshots gets the trend without keeping state of its own.

The `top_senders` rows carry the stored columns (`sender`, `airtime_ms`, `packets`,
`advert_ms`, `first_seen`, `last_seen`) plus what the route resolves for the page:
`name` and `short` for a full public key that the contact tracking knows,
`unattributed` for the reserved bucket, and `advert_percent` — the slice as a percent
of that sender's own airtime, `null` when the slice is unknown.

The `top_emitters` rows carry the same shape for the emitter ledger: `emitter` (a
label, not necessarily a key), the same stored columns, and the same resolved
`name`, `short`, `unattributed` and `advert_percent` — with `name` set for a node
address only when exactly one tracked contact begins with those bytes. The two lists
are two facts about the same copies; they must never be summed.

The `top_relays` rows carry that shape for the entry-relay ledger: `relay` is always a
node-address prefix — a flood path never carries a full key, and a direct copy never
reaches this table — so `unattributed` is never set here, and `name` follows the same
one-match rule. A relay row's absence is not silence: direct copies belong to no row
by construction. The three lists hold the same copies once per fact each — writer,
gateway, antenna — and must never be summed with one another.

All three lists follow the page's history selector when the request carries `?days=N`
(clamped to 1–90): the rows are then the day tables summed over that window,
day-aligned, and the payload says which window it answers with
`"share_window_days"`. Without `days` the cumulative ledgers answer, as they always
did, and no `share_window_days` key is present.

Each row of the three lists also carries `capacity_percent`: that row's share of what
the channel could physically carry over the selected period, rounded to one decimal.
The spectrum is one shared channel and one radio receives one packet at a time, so the
ceiling is one channel-second per elapsed second — the payload exposes it as
`"period_capacity_ms"`, next to `"period_start_ts"`, the first moment of the range
answered: in the period view it is the day-aligned window's start (the selector says
"24 h" while the window covers whole UTC days — the page prints the span it actually
answers for so the percent can be checked), and in the cumulative view the oldest
recorded moment in the ledgers (each station's `first_seen`, the oldest day row, the
oldest sample bucket). A row at 100%
would be one station filling the channel alone; a table summing above 100% is
oversubscription — transmissions running in parallel whose copies this antenna still
paid for. The three tables price the same medium from three ends, so the same ceiling
applies to all three and their percentages are directly comparable — while the ledgers
themselves stay unsummed. The percent is taken after the prefix fold, so one antenna
carries one figure for all its copies, and it is `null` — not `0` — when the period's
elapsed time is unknown (no recorded moment anywhere in the cumulative view).

Each table's header also sums its whole ledger for the period, exposed as
`"period_totals"`: `airtime_ms`, `packets`, `stations`, `advert_percent` and
`last_seen` over every row of that ledger (day rows inside the window, or the whole
cumulative table), plus the sum's own `capacity_percent` against the same ceiling.
The sum is never just the page's slice — the entry count next to it says where the
slice ends — and it is `null` (not 0) when the ledger holds no row for the period.
`advert_percent` is `null` when the period mixes rows from before the advert split:
a share over only the rows that know the split is an invented period figure.

`GET /api/telemetry/occupancy/history?days=7` — buckets as sums and counts, plus the
nearest-rank percentiles. Only buckets that were written appear in `buckets`. Each
bucket also carries `rx_hashed_ms` and `rx_copy_ms` — `NULL` for buckets written before
that split was recorded — and the payload states the period's `copy_percent` (all or
nothing: `null` when any bucket in the range predates the split, because a ratio over
part of the period is an invented period figure). The payload also carries `previous`:
the equal-length period before this one, in the same shape, kept as its own object so
the reader can tell "nothing happened before" (the range was recorded and held nothing)
from "nothing was recorded before" (the range predates retention). The `/airtime` page
draws the comparison from it and states a change only when both periods hold recorded
buckets.

Both accept `?format=text`, which renders one field per line for a monitoring agent.
Only scalar fields are rendered that way; the bucket list and the sender list are not,
so read them as JSON. Both routes are on the anonymous allowlist, like the storage
probe: the payload carries aggregate counters and sender keys that
`GET /api/contacts` already shows.

`enabled` is the setting as the reader knows it, not the value the snapshot happened to
be written under: recording can have been on when the snapshot was written and off now,
which is two facts. The viewer re-reads `enabled` from `config.ini` on every request and
only falls back to the copy it read at boot when the file cannot be read — a hot config
reload never reaches the running viewer process, and a page that tells the reader to set
the flag has to answer from the file the reader would edit. When the setting and the
snapshot disagree the payload also carries `snapshot_enabled`, and
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

## Passive repeater estimates and received-copy analysis

The **Network estimates** link on `/airtime` selects `/airtime?view=network` on
that same route. It adds the compact Baliz view without removing the existing
local-accounting screen, three ledgers, channel floor, API or radio command.
The new view currently uses French labels. Its calendar markers use `[Bot] timezone`, not a hard-coded deployment timezone.

### Recording and API

With `[Channel_Occupancy] enabled = true` and the web-viewer writer running,
existing `packet_stream` events are incrementally imported in batches of at most
1,000. The extra tables contain timestamps, full-frame lengths, SHA-256 payload
fingerprints, paths, modulation, public node identities and opaque scope context.
They do not duplicate message bodies, raw frames, channel secrets or payloads.
An author is resolved from an advert or an RF-linked decoded-message event;
conflicting identities remain unknown. Unknown and ambiguous prefixes are not
silently assigned to a contact.

In the passive view, the source overview puts unattributed traffic first by
default. The checkbox hides that row from the overview without changing any
totals. Each row shows its share of all distinct packets in the selected period;
the displayed airtime and the order of identified sources use reconstructed
radio cost.

Migrations **41–43** append metadata attribution, scope context and local capture
intervals after the existing schema. Metadata uses
`Data_Retention.occupancy_samples_retention_days` (default 30 days); capture
intervals are pruned to 30 days. Historical import uses the modulation known
when importing, because older packet logs do not record the historical profile.

`GET /api/telemetry/occupancy/analysis?window=1h|24h|7d` returns a read-only model
with `schema_version=1` and `method.version=6`, cached per window for 15 seconds.
It carries separate `local`, `traffic`, `authors`, `repeaters`, `receptions`,
`forwarding_model`, `coverage`, `quality` and `limitations` fields. An unavailable
estimate is `null`, not a claim of zero traffic. The route follows the public,
metadata-only telemetry policy of the existing occupancy endpoints.

### Frame airtime and local occupation

For each consistent RF frame:

```text
L = 2 + transport_header_bytes + path_bytes + payload_bytes
transport_header_bytes = 4 for TRANSPORT_* routes, otherwise 0
fingerprint = SHA256(payload_type + ':' + complete_payload_hex)
```

A supplied raw-frame length must agree with `L`. The existing
`channel_occupancy.lora_airtime_ms` prices the complete frame, including preamble,
using the recorded SF, bandwidth, coding rate and preamble length. It implements
the Semtech LoRa symbol formula (explicit header and CRC by default; automatic
low-data-rate optimization at symbol duration >=16.38 ms). This is a calculated
airtime, not a hardware busy-channel or CAD measurement.

**Every received RF copy counts** in local occupation. The average is received
frame airtime divided by the available retained observation span in the selected
period; packet rate uses that same denominator. Average packet size is the mean
complete-frame byte length, not the application text length. A five-minute peak
is calculated independently of chart aggregation. These are receiver-side
observations, not the occupancy of every location in the mesh and not a shared
10% duty-cycle budget. Overlapping transmissions cannot be inferred as a union
of physically busy time from decoded packets alone.

### Reconstructing a repeater's transmissions

Only `FLOOD` / `TRANSPORT_FLOOD` paths are emission evidence. `TRACE` is excluded;
a direct-route path describes future routing instructions rather than a history.
For a flood path `A -> B -> C`, the observer directly heard C, while A and B are
inferred upstream transmitters. Each hop is resolved only when its 1-, 2- or
3-byte prefix matches exactly one known repeater/room server. A known author
also supplies the original emission. Frame airtime is recalculated at each
stage, since each forwarding hop adds path bytes.

Multiple received branches can share one upstream transmission. Evidence is
grouped by full payload fingerprint, path prefix, route, bytes per hop and radio
profile. For each inferred stage:

```text
latest_possible_end = received_frame_end - downstream_frames_airtime
possible_end_interval = [latest_possible_end - 60 s, latest_possible_end]
```

Compatible intervals are intersected to count one shared inferred emission.
Every directly heard emission is retained: two direct anchors never merge.
An inferred emission is timestamped at the resulting upper endpoint. The
60-second forwarding-delay allowance is an explicit modelling assumption;
unobserved same-path retries can still be merged, and inferred timestamps can
move traffic across an hourly boundary. This is reconstruction, not a complete
TX counter.

### Duty-cycle calculation

For a repeater `r` at time `t`, the current estimate is:

```text
DC_r(t) = 100 * sum(airtime_ms of r's reconstructed emissions in (t-3600, t])
                    / 3,600,000
```

The denominator always remains a full hour; it is not scaled up from the short
history available after startup. For example, **36 s attributed in the last hour
= 1% duty cycle**; **360 s = 10% = 6 min/h for that individual emitter**.
The current duty estimate is independent of the graph's selected range.
`last_hour_direct_percent` and `last_hour_inferred_percent` expose its components.
The event-based rolling-hour maximum over the selected period includes a
one-hour lookbehind so the first candidate window is not truncated at the page
filter. This is distinct from an average over a day or a five-minute burst.

Contacts adds this reconstructed rolling-hour estimate to repeater rows/cards.
No attributable emission yields `null` / N/D rather than proof of silence.
The network overview hides directly evidenced first links by default; the
**Afficher les premiers liens** checkbox restores all of them. This is only a
presentation filter, applied to neither stored observations nor model totals.

### Expected retransmission scenario

The separately labelled **Scénario** supplements reconstruction with a bounded
one-hop forwarding model. It learns directed links and forwarding envelopes
from history **before** the evaluation window: up to seven days, at least six
hours of history, at least three supporting packets and recent evidence within
one day. Payloads reused in the evaluation do not train their own prediction.
Known scope, wire profile, payload class and observed hop depth must support
an opportunity. Unresolved scope conflicts remain unknown.

An expected forwarding opportunity is matched against reconstructed evidence;
only unmatched supported opportunities add hypothetical airtime. At most one
additional forwarding per original packet/relay is modelled. Predictions never
seed further predictions. `scenario_hour_percent` stays distinct from the
reconstructed `last_hour_percent`; the overview explicitly labels a scenario
when that larger estimate is displayed. This does not establish packet loss,
FDR/PDR, a repeater's configured flood limit or a compliance verdict. There is no
inverse-FDR multiplier or extrapolation of unseen source traffic.

### Received copies and source roles

Within the selected period the first received copy of each strong fingerprint
is the baseline; all subsequent received copies are additional. The displayed
percentage is **airtime-weighted**, not a packet-count fraction:

```text
additional_share = 100 * additional_received_airtime / all_received_airtime
repeat_factor = received_packets / distinct_payload_fingerprints
```

Copies remain included in occupation and in directly observed emission counts.
Useful flood forwarding is also a copy: this percentage must not be called
"waste" or the duplicate ratio of the entire network. Input truncation makes
the affected ratios unavailable rather than publishing a partial ratio as exact.

Authors are ranked by reconstructed original-plus-relay radio cost. The details
also provide two independent receiver-side roles: the first flood relay where
the copy entered the mesh and the final emitter heard at this observer. These
ledgers answer different questions and must not be added together. Unidentified
identities retain an explicit share.

### Coverage and presentation

A local health recorder joins consecutive healthy writer/radio ticks only.
Restarts, reconnects, stale health, queue loss and clock discontinuities break
continuity. This confirms pipeline availability, not perfect RF reception.
Internal gaps are never filled as silence. A gray baseline dash means a fully
covered bucket with zero received frames; missing data stays blank.

The chart overlays occupation bars and packets/minute, with aggregation adapted
to the selected period: 1-minute bins for 1 h, 15-minute bins for 24 h and
1-hour bins for 7 days. Tooltip values use the same bins. Local midnight markers
and 00:00 axis ticks follow the configured timezone, including DST.

Copy-share trends require two equal, closed and continuously captured windows,
ending at a fresh confirmed capture boundary. Incomplete/truncated observation
is explicitly non-comparable. A single receiver still misses distant traffic,
collisions and undecodable frames: none of these estimates proves regulatory
compliance or describes the whole mesh's RF occupation.
