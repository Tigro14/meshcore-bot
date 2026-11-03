# Vigilance Météo-France Service

Polls the [Météo-France vigilance](https://vigilance.meteofrance.fr) page for one French
department and posts an alert to a channel when the active level reaches Orange or Rouge.
No API key: the page is fetched directly with `requests` and parsed with `beautifulsoup4`.
France only.

Pairs with the [`vigi` command](command-reference.md#vigi-or-vigilance) for on-demand lookups.

---

## Quick Start

1. **Configure Bot** - Edit `config.ini`:

```ini
[Vigilance_Service]
enabled = true
department = 75
channel = #general

# Minimum seconds between two alerts for the same event
alert_throttle = 3600

# Levels that trigger a post
alert_levels = Orange,Rouge
```

2. **Restart Bot** - The service checks Météo-France at `poll_interval` and posts when the
   level is in `alert_levels`.

---

## Configuration

All options live under `[Vigilance_Service]`. See `config.ini.example` for the full list and
comments.

| Option | Description | Default |
|--------|-------------|---------|
| `enabled` | Turn the service on or off | `false` |
| `department` | French department number to monitor (e.g. `75`, `25`) | `75` |
| `channel` | Mesh channel for vigilance alerts | `#general` |
| `poll_interval` | How often to check Météo-France (milliseconds) | `28800000` (8 h) |
| `alert_throttle` | Minimum seconds between successive alerts | `3600` (1 h) |
| `alert_levels` | Comma-separated levels that trigger an alert (`Vert,Jaune,Orange,Rouge`) | `Orange,Rouge` |
| `flood_scope` | Optional regional TC_FLOOD scope, e.g. `#west` | inherits `[Channels]` |
| `silence_mesh_output` | Send only to Discord/Telegram, not to the mesh channel | `false` |
| `discord_webhook_urls` | Optional comma-separated webhook URLs | — |
| `telegram_chat_ids` | Optional comma-separated chat IDs | — |

---

## Features

- **Page parsing**: the color is read from `data-color`/`data-level` attributes first, then
  alert CSS classes (legend/menu blocks are skipped), then compound class prefixes such as
  `vigilance-rouge`, then meta tags, then active-alert text such as
  `vigilance rouge en cours`. Pages with no alert resolve to `Vert`.
- **Department slugs**: common departments map to their page slug (`25` → `besancon`);
  unmapped numbers are used as-is.
- **Throttle**: `alert_throttle` prevents a flood of posts when the level stays elevated
  across polls. Level changes are logged even when no alert is sent.
- **Retries**: transient network errors (connection reset, timeouts, remote disconnects)
  are retried up to 3 times with exponential backoff and jitter; a failed poll is logged and
  the loop continues.
- **Mesh size**: the alert is kept compact (≤ 180 chars) so a single transmission carries it.

**Example alert:**
```
🟠 VIGILANCE ORANGE
Dept 25
Orages
```

The page link is appended for Discord/Telegram (the mesh post stays short).

---

## Notes

- The service scrapes an HTML page, so a Météo-France redesign can change what the parser
  finds. When the level cannot be determined it reports `Vert` rather than guessing.
- Alerts are not persisted across restarts: after a restart, an elevated level triggers a
  new alert on the next poll.
- The bot must have internet access for this service and for `vigi`.
