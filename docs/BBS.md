# BBS — Store-and-Forward Mailbox

The BBS command is a private store-and-forward mailbox between mesh nodes heard by
the bot. One node leaves a message for another; the bot queues it and delivers it when
the recipient contacts the bot. It is **not** a public bulletin board: there are no
rooms, no public posts and no search over stored messages.

## Overview

- Mailboxes are keyed on each node's **full public key** (migration 35, `bbs_inbox`
  table): two nodes advertising the same name get separate mailboxes, and a queued
  message stays deliverable when the recipient renames its node.
- Only nodes the bot has heard are addressable: recipients are resolved from the
  `complete_contact_tracking` table (`role = 'companion'`).
- Reading is **DM-only** for privacy.
- Every limit (mailbox size, sender quota, rate limit) is keyed on public keys, not
  display names.
- Recipients are notified **privately by DM**: on arrival, again after 24h if still
  unread, then a final notice at 73h before the age purge — always aggregated into
  one DM carrying the unread count, never one DM per stored message.

## Commands

| Command | Aliases | Where | What it does |
|---|---|---|---|
| `s <query>` | `send <query>`, `bbs s <query>`, `bbs msg <query>` | DM or channel | Look up a recipient |
| `s <N> <message>` | `bbs s <N> <message>` | DM or channel | Store the message for shortlist entry N |
| `s <hex-prefix> <message>` | `bbs s a1b2c3 <message>` | DM or channel | Store directly for the contact whose public key starts with the prefix (4+ hex characters) |
| `bbs r` | `bbs read` | **DM only** | Read your pending messages |
| `bbs list` | `bbs l` | **DM only** | Count your pending messages |
| `bbs whoami` | `bbs w` | **DM only** | Show the name, key prefix and pending count the BBS uses for you |
| `bbs` | `bbs help` | any | Show the help text |

## Recipient resolution

`<query>` is resolved in this order (same order as the MeshCore-BBS reference
implementation):

1. **Exact contact name** (case-insensitive) — wins outright, even for names that
   look like hex.
2. **Public-key prefix** — only for tokens of 4+ hexadecimal characters.
3. **Partial name match** (name contains the query).

Up to 5 candidates are kept (deduplicated by public key, most recently heard first),
each shown with its key prefix:

```
1 John [a1b2]
2 John [c3d4]
Reply: s <N> <message>
```

The numbered shortlist is valid for **5 minutes**; after that, `s <N>` answers
"No pending recipient list." A hex prefix never needs a shortlist — it resolves in
one step.

## Two-step sending, with examples

### One unique name — sent in one step

A search with exactly one match and a message already on the line sends immediately:

```
> s Tom COD
Message stored for Tom.
```

### Several same-name nodes — pick from the shortlist

```
> s John
1 John [a1b2]
2 John [c3d4]
Reply: s <N> <message>

> s 2 Hey, are you around?
Message stored for John.
```

`[a1b2]` / `[c3d4]` are the first 4 hex characters of each node's public key: they
disambiguate nodes that advertise the same name.

### Straight to a key prefix

```
> bbs s a1b2c3 Meet at noon.
Message stored for John.
```

### Reading the mailbox

```
> bbs r
From Alice [11/05 14:23]: Meet at noon.
From Bob [11/04 09:10]: Still there?
```

Long mailboxes are split per line (see *Delivery guarantees* below).

### Counting and identity

```
> bbs list
You have 2 pending BBS message(s). Send 'bbs r' to read.

> bbs whoami
You are [Tigro] | key 1a2b3c4d | 2 pending
```

`bbs whoami` is useful when two nodes share your name: the key prefix it prints is
what senders must use to reach *your* mailbox (`bbs s 1a2b3c4d <message>`).

### The inbox reminder

Whenever a node DMs the bot for any reason, the bot checks its mailbox and reminds
it about pending messages — unless the DM was itself a BBS command (`bbs`, `s`,
`send`), which answers for itself:

```
📬 You have 1 pending BBS message(s). Send 'bbs r' to read.
```

Two reminders to the same node are spaced by `notify_cooldown_seconds`.

### Private recipient notifications (arrival, +24h, +73h)

Beyond the inbound reminder above, recipients are notified proactively — always by
private DM, always **aggregated** into a single DM carrying the unread count:

| Stage | When | Wording |
|---|---|---|
| 1 — arrival | right after a message is stored (`notify_on_arrival`) | `📬 New BBS message: you have 1 pending. Send 'bbs r' to read.` |
| 2 — reminder | still unread after `notify_reminder_hours` (24h) | `📬 BBS reminder: you still have 2 unread message(s). Send 'bbs r' to read.` |
| 3 — final notice | still unread after `notify_final_hours` (73h) | `📬 BBS final notice: your 2 unread message(s) are deleted after 7 days. Send 'bbs r' now.` |

- Stage 1 fires inside `bbs s` itself and shares `notify_cooldown_seconds` with the
  inbound reminder above, so a burst of stored messages never becomes a burst of
  radio DMs; a message swallowed by the cooldown is still covered by the 24h
  reminder.
- Stages 2 and 3 are swept by a scheduler job every `notify_check_minutes` minutes.
  Each stage is stamped per message in the database (migration 36) **only after the
  DM actually went out on the radio**: a stage is never repeated for the same
  message, it survives bot restarts, and an unreachable recipient is retried on the
  next sweep.
- A reminder counts the recipient's whole unstamped mailbox, so messages that
  arrived after the oldest one crossed the threshold ride along in the same DM.
- Set `notify_reminder_hours` or `notify_final_hours` to `0` to drop that stage;
  both at `0` removes the sweep job entirely.

### Typical rejections

| Answer | Why |
|---|---|
| `No contact found matching 'xyz'.` | The node has not been heard yet, or the query matches nothing |
| `You cannot send a message to yourself.` | Recipient key equals the sender's key |
| `Could not store message for X (mailbox full).` | Recipient already holds `max_messages_per_user` unread messages |
| `Daily BBS quota reached. Try again tomorrow.` | Sender stored `max_messages_per_sender_per_day` messages today (UTC day) |
| `Too many BBS commands — wait a minute.` | Per-user sliding rate limit hit |
| `Pick a number between 1 and N.` | The number is not in the pending shortlist |
| `No pending recipient list. Send 's <name>' to search.` | The shortlist expired (5 min) or was consumed |

## Reading: DM-only, two-phase delivery

`bbs r` refuses group messages ("BBS read is only available via DM").

The whole mailbox is normally answered in one message. When the reply would exceed
150 UTF-8 bytes, each stored message is sent as its own message instead.

Read marks are committed **only after every reply chunk went out on the radio**. If
a chunk fails to send, the bot answers "Send failed — messages kept. Retry 'bbs r'."
and nothing is marked read — a failed send never silently drops messages.

## Limits and internals

| Setting (`[BBS_Command]`) | Default | Behaviour |
|---|---|---|
| `enabled` | `true` | Master switch for the command |
| `max_messages_per_user` | `10` | Unread messages one recipient may hold (0 = unlimited); senders get "mailbox full" |
| `max_message_age_days` | `7` | Messages older than this are deleted from both tables **on the next BBS send** (0-day-age purge effectively disables it — set a large value) |
| `max_messages_per_sender_per_day` | `20` | Messages one sender may store per UTC day (0 = unlimited) |
| `rate_limit_per_minute` | `10` | Sliding 60-second window of BBS commands per user (0 = no limit); applies to recipient lookups and sends |
| `notify_cooldown_seconds` | `300` | Minimum spacing between two inbox reminders to the same node (in-memory; resets on restart) |
| `notify_on_arrival` | `true` | Send the arrival DM to the recipient right after a successful store |
| `notify_reminder_hours` | `24` | Unread messages older than this get one aggregate reminder DM (0 = stage off) |
| `notify_final_hours` | `73` | Unread older than this get the final notice, timed before the age purge (0 = stage off) |
| `notify_check_minutes` | `30` | Minutes between two scheduler sweeps of the reminder/final-notice stages |

Hard-coded internals: shortlist TTL **5 minutes**, shortlist size **max 5**, read
response chunking threshold **150 UTF-8 bytes**, minimum key prefix length **4 hex
characters**.

## Configuration

```ini
[BBS_Command]
# Store-and-forward private mailbox between mesh nodes (keywords: bbs, s, send).
# Mailboxes are keyed on full public keys, so same-name nodes stay separate.
enabled = true
# Max unread messages one recipient may hold (0 = unlimited)
max_messages_per_user = 10
# Messages older than this many days are purged on the next BBS activity
max_message_age_days = 7
# Max messages one sender may store per day (0 = unlimited)
max_messages_per_sender_per_day = 20
# Max BBS commands one user may issue per minute (0 = no limit)
rate_limit_per_minute = 10
# Minimum seconds between two inbox reminders to the same user
notify_cooldown_seconds = 300
# Private notification DMs to the recipient (aggregated: one DM with the
# unread count, never one DM per stored message)
notify_on_arrival = true
# Re-notify after this many hours still unread (0 = no reminder stage)
notify_reminder_hours = 24
# Final notice after this many hours still unread, timed before the age
# purge so the recipient can still read the message (0 = no final notice)
notify_final_hours = 73
# Minutes between two scheduler sweeps of the reminder/final-notice stages
notify_check_minutes = 30
# channels =
```

The command can also be toggled from the web viewer **Plugins** page, which hot-reloads
command settings without a restart.

## Migration and the legacy table

- Migration 35 created `bbs_inbox`; all new messages land there.
- Migration 36 added `reminder_sent_at` and `final_notice_sent_at` to `bbs_inbox`:
  per-message stamps for the +24h reminder and +73h final-notice stages, so a
  stage is sent at most once per message and survives bot restarts.
- The old name-keyed `bbs_messages` table is kept **read-only**: `bbs r`, `bbs list`
  and `bbs whoami` still count and serve its unread rows so nobody loses queued
  mail, and the age purge empties it over time. Nothing is ever written to it again.

## Web viewer integration

The viewer has a BBS statistics page in the main navbar (**BBS**, after Infos) and at
`/bbs`, backed by `GET /api/bbs`:

- totals (pending, stored today/total, delivered, active senders),
- the list of recipients with pending messages, grouped per mailbox — a public-key
  mailbox and a legacy name mailbox keep separate rows even when the display name
  matches,
- pending message ages (< 1 day / 1–3 days / 3–7 days / ≥ 7 days — a non-zero
  "≥ 7 days" bucket means the purge has not run recently),
- top senders and 14-day sent/delivered activity.

Only counts, node names and 4-character key prefixes ever leave the server; message
bodies are never sent to the browser. See [web-viewer.md](web-viewer.md).

## Privacy notes

- Messages are only ever delivered to the recipient's mailbox, readable in DM.
- `bbs r` / `bbs list` / `bbs whoami` are DM-only so a group channel cannot read
  someone's mailbox.
- The web page and its API expose aggregate data only.
- Stored message text does appear in the bot's INFO log line (`BBS: sender -> recipient:`
  with the first 50 characters), as for other commands.

## FAQ

**Why "No contact found matching …"?**
Only nodes the bot has heard are addressable. The node must have been seen on the
mesh (it appears in contact tracking as a companion). A node known only from a single
encrypted packet without a resolved name will not appear.

**My recipient's name is ambiguous.**
Search (`s <name>`) shows every candidate with a key prefix; pick the number, or
send straight to the prefix (`bbs s a1b2c3 <message>`). `bbs whoami` gives a node its
own prefix.

**I picked a number but it says "No pending recipient list."**
Shortlists expire after 5 minutes (and are consumed by one send). Run the search
again.

**Old pending messages disappeared on their own.**
That is the age purge: every stored message older than `max_message_age_days` is
deleted from both tables whenever a new message is sent.

**The web page lists two rows with the same recipient name.**
One is the public-key mailbox (`bbs_inbox`), the other is leftover legacy mail
(`bbs_messages`, no key prefix shown). They are separate mailboxes until the legacy
rows age out.

## See also

- [Command reference — `bbs`](command-reference.md)
- [Web Viewer](web-viewer.md) — the `/bbs` page and `/api/bbs` API
