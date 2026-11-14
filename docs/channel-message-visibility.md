# Channel message visibility

Use **Radio → Channel Management → Live Channel Messages** to choose which
channels may publish decrypted messages on the web. Publication is **off by
default**, including when creating a channel. After upgrading, explicitly enable
each channel you want to show in the Realtime message feed.

The bot continues receiving and processing hidden channels. Commands, local
message storage, statistics and radio analysis still work. This is a web
publication setting, not an encryption-at-rest or local retention setting.

## Consent and history

Consent is persisted separately from the radio channel list and bound to both
the channel name and key. Synchronizing slots preserves consent; replacing a key
or reusing a slot does not grant access to a different channel. Ambiguous channel
names fail closed. Radio channel reads verify the returned slot and retry a
transient timeout or late reply before publishing a refreshed cache snapshot.
Legacy history without a verifiable channel identity is not
replayed.

Visibility is checked on the server before live delivery, history replay and
command-monitor responses. Hidden channel commands cannot expose their input or
answer through the command stream. Changing visibility reloads connected Realtime
pages so their existing message buffers and decoder configuration are cleared.

## Packet stream and logs

The packet stream retains raw ciphertext, packet types, paths, hashes, signal
levels and the other metadata needed for analysis. Server-added decrypted text
is removed before transmission. The browser decoder only receives the labels of
channels explicitly enabled for publication; private channel keys are never
returned by the public channel-list APIs.

Technical logs remain readable. Message processing and sending log metadata
rather than conversation text. Live log delivery and history replay redact
message fields and older conversation dumps, including nested debug structures.
Unprefixed continuation lines are omitted because they can be part of multiline
message text. New technical diagnostics are preserved rather than replaced with
a generic placeholder. Plugins that log conversations must use message metadata
or a recognized content field.

Enabling a channel publishes its messages in **Live Channel Messages**; it does
not duplicate conversations in the log stream. Existing local log files and
stored messages are not erased or rewritten, and retention settings are unchanged.

Publication also decides what the public **Channels** page (`/channels`) may hand
out as a MeshCore *Add Channel* QR code, whose `secret` is the channel key: a
custom channel's code reaches anonymous visitors only while
`live_messages_visible` is on, hashtag channels are always offered because their
key is derived from the published name, and `Public` is never offered. See
[web-viewer.md](web-viewer.md).

## Management API and authentication

- `GET /api/channels` and `/api/channels/live`: visible channel labels only, no keys.
- `GET /radio/api/channels`: full management list, including visibility settings.
- `PUT /radio/api/channels/live-visibility`: strict boolean
  `live_messages_visible`, with `channel_idx` and `channel_name`.
- `POST /radio/api/channels`: channel creation, with publication disabled unless
  `live_messages_visible` is explicitly `true`.

The legacy channel-creation endpoint cannot enable publication. Management routes keep the viewer's authentication and CSRF protections. If authentication is
provided by a reverse proxy, protect `/radio/` as well as the Radio page itself.
Anonymous Socket.IO clients may subscribe to the Realtime traffic streams, but every row on
them passes this policy first: unpublished messages and hidden-channel commands stay on the
server, private bot replies reach admin sessions only, and the Socket.IO log stream remains
admin-only.

Migration 25 adds `channel_web_visibility`; no configuration-file setting or
existing migration is changed.
