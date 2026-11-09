# Vigilance Météo-France Service

Polls the [Météo-France vigilance](https://vigilance.meteofrance.fr) data for one or several
French departments and posts an alert to a channel when the active level reaches Orange or Rouge.
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
# Several departments, comma-separated:
# department = 75, 77, 78, 91, 92, 93, 94, 95, 60, 27
channel = #general

# Optional. Key from portail-api.meteofrance.fr, subscribed to DonneesPubliquesVigilance
# (60 requests/min). Without it the level comes from the site's own endpoint, which covers
# metropolitan France and Corsica only.
# api_key =

# Minimum seconds between two alerts for the same department
alert_throttle = 3600

# Pause between two requests of the same poll (seconds). 0 sends them back to back
request_delay = 2

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
| `department` | French department(s) to monitor, comma-separated: number (`75`, `25`, `95`) or Corsican code (`2A`), or a department name | `75` |
| `api_key` | Optional key from portail-api.meteofrance.fr (`DonneesPubliquesVigilance`); required for the overseas departments | — |
| `channel` | Mesh channel for vigilance alerts | `#general` |
| `poll_interval` | How often to check Météo-France (milliseconds) | `28800000` (8 h) |
| `request_delay` | Pause between two requests of the same poll, in seconds | `2` |
| `alert_throttle` | Minimum seconds between successive alerts | `3600` (1 h) |
| `alert_levels` | Comma-separated levels that trigger an alert (`Vert,Jaune,Orange,Rouge`) | `Orange,Rouge` |
| `notify_on_change` | Announce changes, daily reminders and the return to green; persist successfully sent alerts | `false` |
| `reminder_interval` | Seconds before repeating an unchanged active vigilance in change mode | `86400` (24 h) |
| `offline_cache` | Persist complete bulletins; report failed updates once and answer offline `vigi` queries with dated historical information | `false` |
| `group_similar_alerts` | In change mode, combine departments due an alert with identical colors and phenomena | `false` |
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
  for the default 8 h poll even with a list of departments, and it is the only source for the
  overseas departments.
- **Incomplete data**: a missing session cookie, unknown color, wrong department or missing
  publication date is an error, never evidence of a green level. The JavaScript page markup
  is not a fallback for the structured bulletin. Expired data with a supplied validity end
  is ignored by the monitor and cannot close an active vigilance.
- **Department slugs**: every department resolves to its page slug, which is the department
  name (`95` → `val-d-oise`, `25` → `doubs`, `2A` → `corse-du-sud`). The site has no page for
  a bare number, so an unmapped number is refused with a clear error instead of fetching a
  404; a name typed in config (`department = Val-d'Oise`) is folded to its slug.
- **Several departments**: `department` accepts a comma-separated list. By default, one alert per
  department that is at an alert level, and the throttle is per department, so an alert sent
  for one does not hold the others back. `vigi` without an argument answers for the first
  department of the list, because answering for all of them would not fit the RF budget.
  Request budget per poll: with a key the national product covers the whole list in one
  request; without one the page is fetched once (the `mfsession` cookie is a session cookie, so
  the same token answers for every department) and one request per department follows — a list
  of 10 costs 11 requests, not 20.
- **Pacing**: `request_delay` (default 2 s) is the pause between two requests of the same poll,
  so a list is spread out instead of fired as one burst. The first request of a poll is not
  preceded by a pause and every one after it is, so a list of 10 costs 11 requests and 10 pauses,
  about 20 s — nothing on an 8 h poll. `vigi` is paced by the same key. With an API key there is
  one request per poll, so nothing is spaced. `request_delay = 0` sends the requests back to back.
- **Throttle**: `alert_throttle` prevents a flood of posts when the level stays elevated
  across polls. Level changes are logged even when no alert is sent.
- **Retries**: transient network errors (connection reset, timeouts, remote disconnects)
  are retried up to 3 times with exponential backoff and jitter; a failed poll is logged and
  the loop continues.
- **Mesh size**: legacy alerts are kept compact (≤ 180 chars). Change-mode messages use
  the command manager's byte-aware splitting and radio pacing when needed.

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
  check fails without changing the previous alert state. An overseas department cannot be answered
  without a key, so an overseas code listed in `department` is named in the start-up warning and
  left out of the poll until a key is set. At start the service logs a warning when no key is
  configured.
- In legacy mode (`notify_on_change = false`), the throttle remains in memory. In change mode,
  the last successfully transmitted alert is stored in `bot_metadata`, independently for
  each destination channel/scope and department. A restart does not repeat it immediately.
- Current vigilance requires internet access. With `offline_cache = true`, `vigi` can
  return the last saved bulletin as historical information while updates are unavailable.

## Change-driven alerts

Set `notify_on_change = true` to enable this policy:

- A green department remains silent when no alert was previously transmitted.
- The first configured non-green level generates an alert on the next check.
- A change of overall color or of the active phenomena and their levels generates an
  update on that check, even if `alert_throttle` has not elapsed.
- An unchanged active vigilance is repeated after `reminder_interval` seconds since the
  last successful transmission. A new bulletin timestamp alone is not a change.
- A return to green closes a previously transmitted non-green alert once.
- Failed radio sends do not move the stored state or reminder deadline and can be retried
  on the next check. Radio transmission success is not an acknowledgement from every receiver.

The existing command manager handles radio pacing and message splitting. Human-command
cooldowns are skipped for these automated sends; bot and channel limits remain in force.
No LLM is involved. Failed downloads leave the last announced state intact.

Baliz's Brittany profile uses departments `29,22,35,56`, hourly checks, levels
`Jaune,Orange,Rouge` and channel `#bretagne`, with a 24-hour reminder. `29` stays
first so `vigi` without arguments continues to answer for Finistère.

## Unavailable updates and cached bulletins

Set `offline_cache = true` to keep the complete source JSON, normalized phenomena,
publication date, validity end when supplied, and retrieval time in `bot_metadata`.
The monitor and `vigi` share this cache; it survives a restart. Session cookies, tokens
and request headers are not stored in it. A bulletin with an older publication date
cannot replace a newer one. Missing or expired data cannot become a current bulletin.

- A failed update produces one compact notice naming the affected departments. A source
  failure is reported as “Vigilance non actualisable”; it does not imply the whole internet
  connection is down. Partial success still allows alerts for the other departments.
- Identical failures remain silent, including after restart. If a saved bulletin expires
  during an outage, one further notice marks it as expired. Without a supplied validity
  end, information older than 24 hours is marked as old, rather than given an invented
  official expiration time.
- Offline `vigi` answers show the last known level and phenomena, publication/validity
  dates, and “Situation actuelle inconnue.” They are labelled “Bulletin non actualisé”,
  “Bulletin périmé” or “Bulletin ancien”; no saved green level is presented as current.
- Cached data never triggers normal alerts, daily reminders or a return to green.
  Fresh valid data resumes the normal policy and produces one recovery notice.
- Availability notices are acknowledged only after a successful send. Their persistent
  state is separate from the per-department weather alert state and scoped to the destination.

The full bulletin is retained locally; radio answers remain concise. Checks keep their
configured interval, including during an outage. No additional LLM call is made.

## Grouping similar alerts

Set `group_similar_alerts = true` with `notify_on_change = true`. Departments are grouped
only when an alert is due for each of them and their overall level and complete set of
active phenomenon IDs/levels match. Different risks or different phenomenon levels stay
separate. Red groups are transmitted before orange and yellow groups.

Each department retains its own last successful transmission and reminder deadline.
A failed grouped send advances none of its members. New alerts mixed with reminders are
not labelled as a reminder for the whole group. Fresh returns to green can be combined.

Example: `🟠 Vigilance ORANGE - Depts 22/29/56` followed by the shared phenomenon levels.
The source URLs remain available to external notifications, without adding them to radio posts.

Baliz enables both options for Brittany. The other settings, including hourly checks and
24-hour reminders, are unchanged.
