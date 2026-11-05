# Vigilance Météo-France Service

Polls the [Météo-France vigilance](https://vigilance.meteofrance.fr) data for one French
department and posts an alert to a channel when the active level reaches Orange or Rouge.
France only.

The level never comes from the page markup, because the page is filled by JavaScript. It comes
from the JSON endpoint the site's own JavaScript calls, using the anonymous session cookie the
page sets, or from the official
[DonneesPubliquesVigilance](https://portail-api.meteofrance.fr/web/fr/api/DonneesPubliquesVigilance)
product when `api_key` is set. The key is optional for metropolitan France and Corsica, and
required for the overseas departments.

Pairs with the [`vigi` command](command-reference.md#vigi-or-vigilance) for on-demand lookups.

---

## Quick Start

1. **Configure Bot** - Edit `config.ini`:

```ini
[Vigilance_Service]
enabled = true
department = 95
channel = #general

# Optional. Key from portail-api.meteofrance.fr, subscribed to DonneesPubliquesVigilance
# (60 requests/min). Without it the level comes from the site's own endpoint, which covers
# metropolitan France and Corsica only.
# api_key =

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
| `department` | French department to monitor: number (`75`, `25`, `95`) or Corsican code (`2A`), or a department name | `75` |
| `api_key` | Optional key from portail-api.meteofrance.fr (`DonneesPubliquesVigilance`); required for the overseas departments | — |
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

- **Level source**: the level comes from a JSON answer, with color codes 1 Vert … 4 Rouge.
  Without `api_key` that answer is the endpoint the site's own JavaScript calls
  (`/wsft/v3/warning/full`), and the token is the ROT-13 of the `mfsession` cookie the page
  sets — the same translation the page's JavaScript does. With `api_key` the official product
  is used instead, where the department is read out of the national `periods` →
  `timelaps.domain_ids` block; one call covers every department, so a 60 req/min key is enough
  for the default 8 h poll, and it is the only source for the overseas departments.
- **Page parsing** (last resort, when the endpoint cannot be authenticated): the color is read
  from `data-color`/`data-level` attributes first, then alert CSS classes (legend/menu blocks
  are skipped), then compound class prefixes such as `vigilance-rouge`, then meta tags, then
  active-alert text such as `vigilance rouge en cours`. Pages with no alert resolve to `Vert`.
- **Department slugs**: every department resolves to its page slug, which is the department
  name (`95` → `val-d-oise`, `25` → `doubs`, `2A` → `corse-du-sud`). The site has no page for
  a bare number, so an unmapped number is refused with a clear error instead of fetching a
  404; a name typed in config (`department = Val-d'Oise`) is folded to its slug.
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

- The keyless path is the site's own endpoint, not a published API: the path, the cookie and the
  ROT-13 form of the token can change without warning. If the page does not set the cookie, the
  service falls back to the markup and reports `Vert`. An overseas department cannot be answered
  without a key. At start the service logs a warning when no key is configured.
- Alerts are not persisted across restarts: after a restart, an elevated level triggers a
  new alert on the next poll.
- The bot must have internet access for this service and for `vigi`.
