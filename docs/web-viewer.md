# MeshCore Bot Data Viewer

A web-based interface for viewing and analyzing data from your MeshCore Bot.

## Features

- **Dashboard**: Overview of database statistics and bot status
- **Repeater Contacts**: View active repeater contacts with location and status information
- **Contact Tracking**: Complete history of all heard contacts with signal strength and routing data
- **Config panel**: Structured settings with categorized topics and database tools
- **Plugins page**: Toggle every command and service on/off and edit their settings from the browser. Changes are validated, written to `config.ini` with comments preserved and a timestamped backup, and the bot hot-reloads command settings within a few seconds (service on/off still needs a restart). Template fields (test reply, path reply prefix, greeter messages, multitest result, MQTT weather template) have a `{ }` button that lists the placeholders they take; every one previews the template as you type against sample data, showing what the bot would send (or that it would send nothing) when the template has a mistake; the test reply and path prefix, which take filters, also list them and preview against a 2-byte, a 1-byte and a direct sample message
- **Purging Log**: Audit trail of contact purging operations
- **BBS Mailbox**: Statistics page for the BBS store-and-forward mailbox: pending totals,
  the per-recipient pending list, top senders, message ages and 14-day activity
- **Real-time Updates**: Auto-refreshes every 30 seconds
- **Channel privacy**: Choose which monitored channels publish messages in Realtime;
  see [Channel message visibility](channel-message-visibility.md)
- **API Endpoints**: JSON API for programmatic access

## Quick Start

### Option 1: Standalone Mode
```bash
# Install Flask if not already installed
pip3 install flask

# Start the web viewer (reads config from config.ini)
python3 -m modules.web_viewer.app

# Or use the restart script for standalone mode
./restart_viewer.sh

# Override configuration with command line arguments
python3 -m modules.web_viewer.app --port 8080 --host 0.0.0.0
```

### Option 2: Integrated with Bot
1. Edit `config.ini` and set:
   ```ini
   [Web_Viewer]
   enabled = true
   auto_start = true
   host = 127.0.0.1
   port = 5000
   ```

2. The web viewer will start automatically with the bot

## Configuration

The web viewer can be configured in the `[Web_Viewer]` section of `config.ini`:

```ini
[Web_Viewer]
# Enable or disable the web data viewer
enabled = true

# Web viewer host address
# 127.0.0.1: Only accessible from localhost
# 0.0.0.0: Accessible from any network interface
host = 127.0.0.1

# Web viewer port
port = 5000

# Enable debug mode for the web viewer
debug = false

# Auto-start web viewer with bot
auto_start = false

# Optional: enable the multibyte monitor page and API
multibyte_monitor_enabled = false
```

## Accessing the Viewer

Once started, open your web browser and navigate to:
- **Local access**: http://localhost:5005 (or your configured port)
- **Network access**: http://YOUR_BOT_IP:5005 (if host is set to 0.0.0.0)

### Public and Admin Pages

When `web_viewer_password` is set, the viewer splits into an anonymous read surface and an
admin surface. Everything not on the public allowlist redirects to `/login` (pages) or returns
`401` (APIs).

- **Readable without login**: Dashboard (`/`), Contacts, Mesh Graph, Real-time, Region,
  Airtime, BBS statistics, the Time synchronization page (`/time`, read-only:
  add/delete/toggle/Run Now are hidden for anonymous visitors), the Channels page
  (`/channels`, see below) and Infos. The read-only GET APIs these pages fetch are public too
  (`/api/contacts`, `/api/regions`, `/api/bbs`, `/api/channels/view`,
  `/api/telemetry/occupancy*`, `/api/clock-sync-targets-admin`, `/api/packets/by-hash/<hash>`);
  their payloads carry mesh-visible data only — counts, names, key prefixes, drift and battery
  badges, packet ciphertext with radio metadata, never channel keys, config or unpublished
  message bodies.
- **Admin login required**: Logs, the Settings dropdown (Radio, Scheduled Messages,
  Greeter, Feeds, Region Warnings, Channels, Plugins, Config), the Multibyte monitor, the Time target
  editor (`/time/admin`), every mutating API, per-target sync history (`GET
  /api/clock-sync-targets-admin/<id>/history`), the Run Now action, battery history (`GET
  /api/battery/<key>/history`) and the live Socket.IO log stream. Real-time's live traffic
  streams are open to anonymous sockets but filtered per channel: unpublished messages,
  hidden-channel commands and private bot replies reach admin sessions only.

The viewer signs its admin session cookie with a key kept in `<local_dir>/web_viewer_session.key`
(`0600`, beside `local/config.ini`), created on the first start and reused afterwards. That is what
lets a login survive `systemctl restart`, a deploy or a reboot. A viewer that cannot read or write
that file still starts — it runs on a key for that process only and logs a warning, which means its
logins end at the next restart. Deleting the file invalidates every outstanding login.

## Reverse Proxy With Nginx Basic Auth

If you expose the web viewer outside your local network, run it behind HTTPS and authentication. One option is to bind the viewer locally, then put Nginx in front with basic auth:

```ini
[Web_Viewer]
enabled = true
auto_start = true
host = 127.0.0.1
port = 8080
```

Example Nginx server block:

```nginx
server {
  # [...]
  auth_basic           "Login required";
  auth_basic_user_file /etc/nginx/.meshcore-bot.htpasswd;

  location / {
    # Local web viewer instance
    proxy_pass      http://127.0.0.1:8080;
    proxy_buffering off;
    include /etc/nginx/proxy_params;
  }

  # Socket.IO websocket path for live updates
  location /socket.io/ {
    if ($http_connection !~* "upgrade") {
      return 403;
    }
    if ($http_upgrade !~* "websocket") {
      return 403;
    }

    proxy_pass http://127.0.0.1:8080;
    include /etc/nginx/proxy_params;

    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 86400;
  }
}
```

With the config above, `/etc/nginx/proxy_params` should include the standard forwarded headers:

```nginx
proxy_set_header Host $http_host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
```

## Pages Overview

### Dashboard
- Health strip: bot status, database size, uptime, connected clients, radio state,
  and snapshot age with a manual refresh control
- **Mesh**: nodes heard, adverts, new nodes, nodes gone quiet, and geographic
  coverage — the count tiles carry a 30-day sparkline and a change chip
- Routing mix (flood vs direct), a hop-distance chart, and the role mix
- Path encoding: multibyte share among contacts and among incoming packets,
  plus a 30-day stacked bar whose height is the share of each day's packets that
  took a multibyte path, split by the payload type carrying them (`GRP_TXT`,
  `RESPONSE`, `REQ`, `PATH`, `TXT_MSG`, `ANON_REQ`, `GRP_DATA`, `ADVERT`, and
  `Other` for the rest). Its y-axis is the tallest bar rounded up to the next
  5%, so it rescales as the mesh changes
- Busiest repeaters, and **one-hop neighbours** (radios heard directly; 24-hour
  or 7-day window)

The live packet feed lives on the **Real-time** page rather than here; the
dashboard reads a single snapshot per poll and holds no streaming
subscriptions.

Two measurement notes for the mesh charts:

**Hops are derived, and the two path tables disagree on units.**
`observed_paths.path_length` is a *byte* count, so hops are
`path_length / bytes_per_hop` — with 2- or 3-byte encoding a three-hop path is
six or nine bytes long, and charting the raw value would overstate distance two-
to threefold on a mesh that is ~95% multibyte. `packet_stream.path_len`, by
contrast, is *already a hop count*, with its byte length carried separately as
`path_byte_length`. Applying either table's rule to the other silently rescales
the axis, so both conventions are pinned by tests.

The hops chart shows both distributions: nodes by their closest advert path (7
days) and arriving flood packets by distance travelled (whatever
`packet_stream` retains, typically 3 days). One counts nodes and the other
packets, so each is drawn as a share of its own total. Flood packets carry no
sender identity, which is why they cannot be reduced to a shortest path per
node the way adverts can.

Flood hop buckets holding under 0.1% of the series are omitted, because that
tail decays over roughly twenty hops in bars thinner than a pixel. The number of
packets withheld is printed beneath the chart, and percentages stay shares of
the full series so that hiding the tail cannot inflate the remaining bars. The
node series is shown in full.

**Neighbour membership is direct RF (MeshCore hop count 0).**
An empty path means this radio heard the originator on the air. A path whose
byte length equals `bytes_per_hop` already contains one hop hash — that
originator is one repeater away, not a neighbour. The dashboard lists empty-path
adverts in `observed_paths` plus in-window rows from `neighbor_links` (zero-hop
node-discover). `complete_contact_tracking.hop_count` is not used for
membership: on a representative database it claims 800 zero-hop contacts while
only a few dozen have any empty-path advert to corroborate it, and the SNR
stored against them clusters in a ~1.5 dB band with RSSI near -45 dBm — one
strong local link recorded against every node whose traffic arrived through it.
SNR/RSSI on the panel come from the zero-hop path row or from discover (SNR
only). A relayed packet's SNR measures the last hop into this radio, never the
link to whoever sent it.
- **Bot**: messages, commands, reply rate, and unique users, plus the top
  commands/users/channels and longest paths
- Live activity feed

The dashboard is served from a **snapshot**, not from live aggregation. A
background thread in the viewer process recomputes it every
`dashboard_snapshot_interval_seconds` (default 60) and writes two tables:

| Table | Contents |
|-------|----------|
| `daily_rollup` | One row per local date. Retains counts whose raw sources are pruned long before the dashboard's 30-day window. Signal metrics are stored as sums and counts, never means, so any window re-aggregates correctly. |
| `dashboard_snapshot` | A single JSON row describing current state. |

Two consequences worth knowing:

- **Gaps are not zeros.** A day with no source data stores NULL and renders as a
  break in the line. Days seeded when the feature was first enabled have real
  advert counts but no message, command, path, or packet figures — those raw
  rows were already pruned and cannot be recovered.
- **Window labels come from retention.** The time-window selectors are built
  from each source's configured retention, so the list cannot offer "30 days"
  against a table pruned at 7.

The **multibyte share** trends are accumulated forward, not derived.

The per-payload-type figures come from `packet_stream`, which is pruned within
days while the chart spans thirty, so each day's split is written into
`daily_rollup.packet_type_encoding` as that day is rolled up and cannot be
recomputed afterwards. Enabling the feature therefore starts an empty chart that
fills in over the following month. Packets whose denormalized dimensions have
not been backfilled yet are excluded from both sides of the ratio rather than
counted as single-byte — counting them would invent a dip in whichever type the
backfill has not reached.

**The chart measures the day, and the API serves per-type adoption.** A bar's
height is the share of that day's packets that went multibyte, and its segments
are each type's multibyte packets over that same day-wide denominator — so the
segments sum to the bar and the bar equals the figure the packet doughnut
reports for its own window. Every payload type is counted, with the uncharted
tail (`ACK`, `TRACE`, unmapped ordinals — about 0.8% of live traffic) summed into
`Other`; omitting it would leave bar heights a share of the charted types rather
than of the day.

The `multibyte_share_*` metrics answer the different question "how much of *this
type* went multibyte?", each a ratio over its own denominator. Eight such ratios
share no denominator and cannot be stacked, which is why the dashboard payload
carries the raw counts in `packet_encoding` rather than the eight percentage
series; the tooltip quotes both readings per segment.

**Two different advert shares exist, and they do not agree.** The charted
`multibyte_share_advert` counts advert *packets* off the packet stream, the same
way as every other line. The older `multibyte_share` counts a day's adverts
against a classification of the *node* that sent them — one multibyte path ever
observed marks that node multibyte for every advert it sends. Neither is wrong;
they answer different questions, and the gap between them is roughly the set of
nodes that can do multibyte but mostly do not.

That older share is also frozen for a different reason. `observed_paths` is
deduplicated with a lifetime `observation_count` and a `last_seen` that is
bumped on every re-observation, so historical per-day shares cannot be
reconstructed from it — nearly half the observation volume would be attributed
to the wrong day. Each refresh recomputes today plus a three-day trailing
window; older days stay frozen at the value recorded then.

### Real-time

One tape of what the bot's radio is hearing, newest first. It is readable without login.
What anonymous visitors see is bounded by the same consent rules as every other public
surface: messages and commands only on channels marked publishable, private bot replies
and hidden-channel commands reserved to admin sessions, and packets as ciphertext plus
radio metadata — never server-side plaintext.

- **Receiver strip.** Whether the bot is listening (or reconnecting, or the radio is offline or not responding), when it last heard a packet, packets per minute, and a bar per 30 seconds for the last 10 minutes. Time before the page saw its first packet is drawn as a hairline, not as quiet.
- **One row per transmission.** A packet, the message decrypted from it and a command it triggered share one row, joined by packet hash. The bot's reply hangs under the command with how many times a repeater was heard echoing it back; those echoes don't get rows of their own. A packet heard several times is one row with a count, and opening it lists every reception with its SNR, RSSI, hops and path. Where no hash ties a command to its message (rows stored before the hashes existed), they are joined only when sender, channel and text match exactly within 20 seconds.
- **Color.** The chip at the left of each row is the packet type's icon in the color the dashboard's packet-type chart uses; decoded messages get a speech bubble on a stronger tint, and the bot's own commands a solid violet robot, with its reply in a violet panel underneath.
- **Decoding in the browser.** Advert names, and group-text messages on channels with a configured key (including `decode_hashtag_channels`), are decoded on the page and marked "decoded in this browser". Other group text shows its channel hash and "no key here".
- **Holding still.** New rows queue behind an "N new" button while the tape is paused (`P`), scrolled down, under a moving mouse pointer (for up to 5 seconds of stillness) or holding keyboard focus.
- **Keep, filter, search.** The star keeps a row in a tray above the tape, safe from the 300-row limit and from **Clear tape**. Filters narrow to messages or commands, a packet type or a channel; `/` jumps to search across sender, text, repeater prefixes and hash.
- **Inspect bytes** on an opened row shows the color-coded byte breakdown, with header bits, the path (hop count × bytes per hop), and GroupText or advert fields.

Rows replayed from the database when the page opens sit under "Heard before you opened this page". The page keeps 300 rows; the server replays the newest 50 of each kind.

The replay arrives as one payload of 50 packets, which a browser handles. A client built on
python-socketio does not: `engineio.payload.Payload.max_decode_packets` is 16, so a payload
carrying more than 16 packets makes that client abort with "Unexpected packet from server,
aborting". The limit is in the client library, not in this server, so a script that
subscribes to the packet stream gets nothing when the replay is 50 rows — keep such a client
under 16 replayed rows, or read the raw polling response instead of the library's decoder.
Two other things bite the same kind of script: handlers registered before `connect()` make
python-socketio join a namespace named after the handler instead of the root one, where the
server's handlers live, and `41[...]` is a disconnect packet rather than an event, so an
event has to be sent as `42[...]`.

### Repeater Contacts
- Active repeater contacts
- Location information (city/coordinates)
- Device types and status
- First/last seen timestamps
- Purge count tracking

### Contact Tracking
- Complete history of all heard contacts
- Signal strength indicators
- Hop count and routing information
- Advertisement data
- Currently tracked status
- The **View advertisement data** action shows the contact's raw advert payload next to a QR
  code carrying `meshcore://contact/add?name=…&public_key=…&type=…` — the deep link the MeshCore
  mobile app scans to add that node as a contact (MeshCore → Admin → Add Node → Scan QR). The key
  is the contact's own 64-hex public key, which the page already prints, and `type` is the
  contact's role mapped to MeshCore's numbering (1 Companion, 2 Repeater, 3 Room Server,
  4 Sensor) — the same table the bot's own `contact` command uses for its card. Nothing is
  encoded for a contact whose key is not 64 hex characters.
- Region-code status: a red globe badge next to the name flags a contact whose channel
  messages were received as an unscoped FLOOD, i.e. sent without a region code (see
  [Region scopes](region-scopes.md)). Only `global` counts as evidence — `unknown` means the
  transport code was never seen for that packet, and a DM has no flood scope — so a contact
  with a region observed, or with no channel message in the window, gets no badge. The
  tooltip gives the count, the window it was measured over and when the last unscoped
  message was heard. `Filters → Region code` narrows the list to those contacts, and both
  the badge and the filter use the window the page's "Heard in" select is set to.

### Battery Level
When `[Battery_Monitor]` is enabled (see [Command reference](command-reference.md#schedule)),
each polled device's contact row shows a coloured voltage badge (green/yellow/red,
thresholds configurable via `low_voltage`/`critical_voltage`), e.g. `4.10V / 92%`.
The percentage is a rough estimate (linear 3.0V=0% / 4.2V=100%), the same mapping
the companion firmware itself uses for its own screen battery icon — not an exact
state-of-charge, but consistent with what the device's own display already shows.
Click the badge for a
7-day rolling chart of that device's voltage history. Polling reuses the
`[Clock_Sync_Admin]` target list — there is no separate target configuration for
battery monitoring. The same badge also appears in the Time page's target table
(same devices, same data). Clicking **Run Now** on the Time page triggers an
immediate battery poll alongside the clock-sync run, instead of waiting for the
next hourly tick.

### Config
- Categorized configuration topics in a left navigation column
- Core settings such as notifications, log rotation, backup, and maintenance status
- Database operations and database information views in the same tab

### Radio
- Radio connect/disconnect and channel management (create, inspect, delete)
- **Radio Parameters**: read/write frequency, bandwidth, spreading factor, coding
  rate, and TX power on the device
- **Node Settings**: read/write companion firmware settings on the device
  - *Response Path Hashing*: the path hash size the firmware uses for each hop
    when building outgoing/response paths (mode 0–2 = 1–3 bytes per hop; larger
    hashes avoid relay collisions but need firmware 1.14+ mesh-wide)
  - *Default Region Scope*: the radio's own default region, stored in firmware
    (`NodePrefs.default_scope_name` / `default_scope_key`) and used for any send
    the bot does not scope itself. Max 30 ASCII characters including the `#`,
    which is added if you omit it; blank clears it and the radio floods
    unscoped. Firmware without the setting is reported as such rather than
    shown as an empty field, and a stored key that is not the stored name's
    hash is flagged, because the radio routes by the key. Note that once the
    bot sends a scoped message it leaves the radio in forced-unscoped mode, so
    this default stops applying until the bot scopes another send — to have the
    bot post under a region, use Region Scopes below
  - *Identity & Adverts*: node name, advertised latitude/longitude, advert
    location policy, and buttons to send a zero-hop or flood advert. The name
    field is locked when the bot manages it (`[Bot] bot_name` with
    `auto_update_device_name` on)
  - *Mesh Behavior*: extra ACK count and telemetry permissions
    (base/location/environment, each deny / per-contact flags / allow all).
    New-contact handling is shown read-only — it is owned by
    `[Bot] auto_manage_contacts` in config.ini, which the bot applies to the
    device itself
  - *Advanced Tuning*: RX delay base and airtime factor (write-only; the device
    does not report current values)
- **Region Scopes**: the bot's own regional flood scopes, edited here rather
  than by hand in `config.ini`. Unlike the cards above it, this writes
  `[Channels]` in `config.ini` and queues a hot config reload — nothing is read
  from or written to the radio. This is a different setting from the radio's
  Default Region Scope above: this one decides what the bot sends and answers,
  that one is the radio's fallback for anything the bot leaves unscoped
  - *Which scopes the bot replies to*: `flood_scopes`. Left empty (the default),
    the bot still answers channel commands, but replies go out as ordinary
    global FLOOD and do not mirror the sender's region. An allowlist of named
    regions enables mirroring for those names (for example `*,#home`); the
    "also reply to unscoped messages" box is the `*` entry that keeps unscoped
    FLOOD allowed. The bot matches inbound transport codes by HMAC against
    those names and cannot recover a region name from the 16-bit code alone
  - *Default outgoing scope*: `outgoing_flood_scope_override`, used for sends
    that carry no scope of their own (scheduled messages, feeds, webhooks) and
    for replies whose incoming scope could not be matched. Blank means global
    flood. A mirrored reply scope still wins over it
  - Per-channel `flood_scope.<channel>` entries are listed read-only, because
    they override the default on their own channel; edit those in `config.ini`
  - Names are normalized the way the bot normalizes them, so `west` is stored
    as `#west`. A name containing `,`, `%` or an inner `#` is refused: the
    first separates the list, the second breaks every later read of the section
    (config.ini is parsed with interpolation on), and the third is the scope
    marker itself
  - The page reports what the bot did with the edit, not just that the file was
    written — it polls the queued reload and says so plainly when nothing
    picked it up
- Device writes are queued through the bot process (`channel_operations` table),
  so the bot must be running and connected to the radio for reads/writes to
  complete

### Channels
- The monitored channels (`[Channels] monitor_channels`) as a card grid, each with the
  MeshCore "Add Channel" QR code a visitor can scan from the MeshCore mobile app
  (`meshcore://channel/add?name=…&secret=…`), plus a button to copy the URI
- Public page: it is on the anonymous allowlist, so it works without a login, and the QR
  exposure is limited accordingly
  - Hashtag channels are always encoded: their secret is the first 16 bytes of
    `SHA256("#name")`, which anyone can derive from the published name, so the QR leaks
    nothing new
  - A custom (non-hashtag) channel's secret *is* its private key. Its QR is only given to
    an anonymous visitor when the operator marked that channel publishable on the Radio page;
    otherwise the channel is left out of the anonymous feed entirely rather than shown
    name-only, because a join nobody can complete is worse than no join
  - An authenticated operator also sees the private keys of unpublished custom channels
  - The `Public` channel is never encoded: its key is fixed by MeshCore rather than
    hashtag-derived, and it is not this bot's channel to distribute
- A monitored channel the radio does not subscribe to yet is still listed, with its hashtag QR
  available, since the QR is derived from the name alone
- Channels the bot only *lists* are listed here too, and badged `décodé`: the names of the
  `[Channels_List]` section it publishes through the `channels` command, and
  `[Web_Viewer] decode_hashtag_channels`. The radio holds no slot for them, so the page says so
  rather than letting a card with a QR read as a monitored channel
- Editing the list happens on `/channels/settings` (in the Settings dropdown, admin only);
  see [Channel Monitoring Settings](#channel-monitoring-settings)
- QR codes are drawn in the browser by the vendored `qrcode-generator` library, so the page
  needs no network access and no server-side image generation

### Channel Monitoring Settings
- Edits `[Channels] monitor_channels` in `config.ini`, the list of channels the bot answers on
- Add by name (`#fr-idf` or `fr-idf`; the `#` is kept for hashtag channels because that is how
  the bot compares names against incoming messages), remove with the row's button, and reorder
  with the up/down controls
- A save writes the file, queues a hot config reload, and reports what the bot actually did
  with it — including plainly saying so when nothing picked the edit up
- Monitored hashtag channels are provisioned automatically: each one the radio does not already
  hold is queued onto a free channel slot (`[Bot] max_channels` slots), with no key sent so the
  firmware derives the hashtag key itself. Slots that are all taken are reported as
  not provisioned rather than silently dropped
- Monitored custom channels are *not* created here: choosing a private key is a deliberate act,
  so the page reports them and points at the Radio page
- Removing a channel stops the bot answering it but leaves the radio slot in place, for the same
  reason; the Radio page is where slots are deleted
- The `Public` channel is refused unless `[Bot]
  i_understand_that_running_the_bot_on_the_public_channel_is_potentially_disruptive_to_other_users_enjoyment_of_the_mesh_and_i_would_like_to_do_it_anyway`
  is `true`, matching what the bot refuses at startup
- Names containing a space, a comma, a `%` or a newline are refused before the write: the comma
  would split the name into a channel nobody asked for, and `%` breaks every later read of the
  section because config.ini is parsed with interpolation on
- The bot matches `monitor_channels` against the wire name verbatim, so the stored list keeps
  the `#` where the channel has one and stays lowercase

### Purging Log
- Audit trail of contact purging operations
- Timestamps and reasons
- Contact names and public keys

### BBS Mailbox
- Statistics for the BBS store-and-forward mailbox (`bbs` commands), covering both the
  pubkey-keyed `bbs_inbox` table and the legacy `bbs_messages` table until the latter ages out
- Six summary tiles: pending messages, recipients with pending, stored today, stored total,
  delivered, active senders
- Pending-recipient table: one row per mailbox, grouped by public key (legacy rows by name,
  so a key mailbox and a legacy name mailbox with the same display name stay separate),
  with the pending count, oldest/newest timestamps and an age column
- Pending age buckets (< 1 day, 1–3 days, 3–7 days, ≥ 7 days); a non-zero "≥ 7 days" bucket
  means the TTL purge has not run recently
- Top 10 senders and a 14-day sent/delivered daily series
- Read-only `[BBS_Command]` settings card; the page auto-refreshes every 30 seconds and has
  a manual Refresh button
- Privacy: the page and its API expose only counts, node names and 4-character key prefixes;
  message bodies never leave the server
- The main navbar shows the entry (after Infos) only while `[BBS_Command] enabled = true`
  (the default); it is a statistics page, not a settings page, so it does not sit in the
  Settings dropdown

## API Endpoints

The viewer also provides JSON API endpoints:

- `GET /api/region-warnings` - Region-code settings, per-channel scope tallies,
  the daily series, today's warning budget, and recent warning decisions
- `POST /api/region-warnings/settings` - Save `[Region_Warnings]` and queue a
  hot config reload
- `GET /api/region-scopes` - Regional flood scopes as the bot resolves them:
  the `[Channels] flood_scopes` allowlist split into named scopes and the
  global flag, the default outgoing scope, any per-channel overrides, and which
  file a save would write
- `POST /api/radio/firmware/config/write` - Queue a firmware write.
  `path_hash_mode` (0-2) and `default_flood_scope` (a region name, or empty to
  clear the radio's default) may be sent together or alone.
  `GET`-side values come back from `POST /api/radio/firmware/config/read`
- `POST /api/region-scopes` - Save `[Channels] flood_scopes` and
  `outgoing_flood_scope_override`, and queue a hot config reload. Returns the
  reload's `channel_operations` id so the caller can poll
  `/api/channel-operations/<id>` for what the bot actually did
- `GET /api/channels/view` - The `/channels` page payload: every monitored, radio-held and
  bot-listed channel with its join QR (`qr.name`, `qr.secret`, `qr.uri`),
  `monitored`/`on_radio`/`decoded`/`published` flags, and a `share_reason` when a channel
  carries no QR. `decoded` marks a name that comes from `[Channels_List]` or
  `[Web_Viewer] decode_hashtag_channels` rather than from a radio slot. Public, so the QRs are
  consent-limited: hashtag channels always, custom channels only when published on the Radio
  page, `Public` never. An authenticated operator's response adds `is_admin: true` and the
  private keys of unpublished custom channels
- `GET /api/channels/settings` - Admin only. `[Channels] monitor_channels` as a
  list, the merged channel view (which entries the radio holds and in which slot),
  and `limit`, the free-slot count `[Bot] max_channels` allows
- `POST /api/channels/settings` - Admin only. Save `monitor_channels` (a list of
  names, `#` optional) and queue a hot config reload. Refuses a list longer than
  `max_channels` slots, an invalid name, and `Public` without the `[Bot]` override
  key. Returns `reload_operation_id` plus `provisioned` (with each queued
  `channel_operations` id and slot), `not_provisioned`, `custom_monitored` and
  `radio_only`, so the page can report what the bot did
- `GET /api/dashboard/summary` - Snapshot-backed dashboard payload, including
  30-day sparkline series and change figures, plus `packet_encoding`: 30 days of
  raw per-payload-type multibyte/total counts for the stacked encoding chart.
  Sends a strong `ETag`; poll with `If-None-Match` to get a bodyless `304` while
  the snapshot is unchanged.
- `GET /api/dashboard/series?metric=<m>&days=<n>` - Full-history points for one
  metric. `metric` is one of `messages`, `commands`, `adverts`, `nodes`,
  `new_nodes`, `packets`, `multibyte_share` (adverts), or the per-payload-type
  packet shares `multibyte_share_grp_txt`, `multibyte_share_response`,
  `multibyte_share_req`, `multibyte_share_path`, `multibyte_share_txt_msg`,
  `multibyte_share_anon_req`, `multibyte_share_grp_data`,
  `multibyte_share_advert`.
- `GET /api/dashboard/top?kind=<k>&window=<w>&limit=<n>` - One leaderboard.
  `kind` is one of `users`, `commands`, `channels`, `paths`, `repeaters`. The
  response carries `window_label`, `retention_days`, and
  `truncated_by_retention`.
- `GET /api/dashboard/windows` - Selector options derived from each source's
  retention.
- `POST /api/dashboard/refresh` - Force a snapshot recomputation.
- `GET /api/stats` - **Deprecated.** The whole-database statistics payload the
  dashboard used to call five times per page load. Every key name is preserved
  for external consumers, and the response carries `Deprecation` and `Sunset`
  headers. Use the `/api/dashboard/*` endpoints instead; this one is removed at
  the next major version.
- `GET /api/contacts` - Repeater contacts data. The contacts page uses optional
  `page`, `page_size` (maximum 200), `search`, `sort`, and `direction` parameters;
  callers that omit pagination retain the legacy full-list response.
- `GET /api/tracking` - Contact tracking data
- `GET /api/bbs` - BBS mailbox statistics: the live `[BBS_Command]` settings, totals
  (pending, recipients with pending, stored today/total, delivered, active senders), the
  pending-recipient list grouped per mailbox (pubkey mailboxes and legacy name mailboxes
  stay separate), top senders, pending age buckets and 14-day daily sent/delivered
  activity. Counts, names and 4-character key prefixes only — never message bodies. With
  neither BBS table present it answers zeroed totals and `error: "BBS tables not found"`.

Example usage:
```bash
curl http://localhost:5000/api/dashboard/summary
```

## Database Requirements

The viewer uses the same database as the bot by default (`[Bot] db_path`, typically `meshcore_bot.db`). That single file holds repeater contacts, mesh graph, packet stream, and other data so the viewer can show everything.

**Dashboard stats** (message/command counts, top users, etc.) come from the stats tables (`message_stats`, `command_stats`, `path_stats`). Stats collection is enabled by default with `[Stats_Command] collect_stats = true`, even if the user-facing `stats` chat command is disabled with `enabled = false`. Set `collect_stats = false` only if you want to stop writing those dashboard stats tables.

## Migrating from a separate web viewer database

If you previously had the web viewer using a **separate** database (e.g. `[Web_Viewer] db_path = bot_data.db`), you can switch to the shared database so the viewer shows repeater/graph data and uses one file.

1. **Stop the bot and web viewer** so neither has the databases open.

2. **Optionally preserve packet stream history** from the old viewer DB into the main DB:
   - From the project root, run:
     ```bash
     python3 migrate_webviewer_db.py bot_data.db meshcore_bot.db
     ```
     Use your actual paths if they differ (e.g. full paths or different filenames). The script copies the `packet_stream` table from the first file into the second and skips rows that would duplicate IDs.
   - If you don’t care about old packet stream data, skip this step; the viewer will create a new `packet_stream` table in the main DB.

3. **Point the viewer at the main database** in `config.ini`:
   ```ini
   [Web_Viewer]
   db_path = meshcore_bot.db
   ```
   (Or the same value as `[Bot] db_path` if you use a different path.)

4. **Start the bot (and viewer as usual)**. The viewer will now read and write to the same database as the bot.

You can keep or remove the old `bot_data.db` file after verifying the viewer works with the shared DB.

## Troubleshooting

### Web viewer not accessible (e.g. Orange Pi / SBC)

If the viewer does not load from another device (e.g. from your phone or PC while the bot runs on an Orange Pi), work through these steps on the Pi.

1. **Confirm config**
   - In `config.ini` under `[Web_Viewer]`:
     - `enabled = true`
     - `auto_start = true` (if you want it to start with the bot)
     - `host = 0.0.0.0` (required for access from other devices; `127.0.0.1` is localhost only)
     - `port = 8080` (or another port 1024–65535)
   - Restart the bot after changing config.

2. **Check that the viewer process is running**
   ```bash
   # From project root on the Pi
   ss -tlnp | grep 8080
   # or
   netstat -tlnp | grep 8080
   ```
   If nothing listens on your port, the viewer did not start or has exited.

3. **Inspect viewer logs**
   - When run by the bot, the viewer writes to:
     - `logs/web_viewer_stdout.log`
     - `logs/web_viewer_stderr.log`
   - Look for Python tracebacks, "Address already in use", or missing dependencies (e.g. Flask, flask-socketio).
   - Optional: run the viewer manually to see errors in the terminal:
     ```bash
     cd /path/to/meshcore-bot
     python3 modules/web_viewer/app.py --config config.ini --host 0.0.0.0 --port 8080
     ```

4. **Check integration startup**
   - Bot logs may show: `Web viewer integration failed: ...` or `Web viewer integration initialized`.
   - If integration failed, the viewer subprocess is never started; fix the error shown (e.g. invalid `host` or `port` in config).

5. **Firewall**
   - Many SBC images (e.g. Orange Pi, Armbian minimal) do **not** ship with a firewall; if `curl` to localhost works and `host = 0.0.0.0`, the blocker may be network (Wi‑Fi client isolation, different subnet, or router). Check from a device on the same LAN using `http://<PI_IP>:8080`.
   - If your system uses **ufw**:
     ```bash
     sudo ufw status
     sudo ufw allow 8080/tcp
     sudo ufw reload
     ```
   - If `ufw` is not installed (e.g. `sudo: ufw: command not found`), you may have no host firewall—that’s common on embedded images. To allow the port with **iptables** (often available when ufw is not):
     ```bash
     sudo iptables -I INPUT -p tcp --dport 8080 -j ACCEPT
     ```
     (Rules may not persist across reboots unless you use a persistence method for your distro.)
   - If you prefer ufw, install it (e.g. `sudo apt install ufw`) and use the ufw commands above.

6. **Test from the Pi first**
   ```bash
   curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8080/
   ```
   If this returns `200`, the viewer is running and the issue is binding or firewall. If you use `host = 0.0.0.0`, then try from another device: `http://<PI_IP>:8080`.

7. **Standalone run (no bot)**
   - To rule out bot integration issues, start the viewer by itself (same config path so it finds the DB):
     ```bash
     python3 modules/web_viewer/app.py --config config.ini --host 0.0.0.0 --port 8080
     ```
   - If `restart_viewer.sh` is used, note it binds to `127.0.0.1` by default; for network access run the command above with `--host 0.0.0.0` or edit the script.

### Flask Not Found
```bash
pip3 install flask flask-socketio
```

### Database Not Found
- Ensure the bot has been run at least once to create the databases
- Check file permissions on database files

### Port Already in Use
- Change the port in `config.ini` or stop the conflicting service
- Use `ss -tlnp | grep 8080` or `lsof -i :8080` (if available) to find what's using the port

### Permission Denied
```bash
chmod +x restart_viewer.sh
```

### A navigation link is missing after a deployment
The navigation bar is rendered from the deployed template, and pages reference their
assets by plain path (no fingerprint, no version query), so a page a browser kept from
before a deployment can show a navigation bar from before it. Static files are not the
problem: Werkzeug serves `static/` with an `ETag` and `no-cache`, so a reused page still
fetches fresh JS and CSS. Force a reload (Ctrl+Shift+R), or open the page in a private
window, and the current navigation appears. Rendered HTML now answers with
`Cache-Control: no-cache`, so a reused page has to revalidate it instead of trusting its
copy; endpoints that deliberately cache their payload (`/api/telemetry/occupancy`, the
mesh and tracking feeds) still set their own `Cache-Control`.

## Security Notes

- The web viewer is designed for local network use
- Set `host = 127.0.0.1` for localhost-only access
- Set `host = 0.0.0.0` for network access (use with caution)
- For network access, set `web_viewer_password` or use a reverse proxy with authentication and firewall rules
- Rendered pages and redirects are sent with `Cache-Control: no-cache`: they carry per-request
  state (a CSP nonce, the session's admin flag, the deployed template) that must not survive in
  a browser copy

## Future Enhancements

- Interactive contact management
- Export functionality
- Additional authentication options
- Mobile-responsive design improvements
