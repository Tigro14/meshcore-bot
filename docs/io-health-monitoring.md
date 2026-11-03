# Storage monitoring and remote power cycle

`[IO_Health]` detects a storage fault when the kernel remounts a filesystem read-only:
the bot counts `EROFS` write failures on the paths it is allowed to write to
(`writable_prefixes`) and raises the shared-memory reboot semaphore once
`failure_threshold` is reached.

That local watchdog is not enough when the disk itself is dead: nothing on the host
survives long enough to reboot it. The web viewer therefore exposes two routes — one
that reports the current state for an external monitoring agent, and one that triggers
the recovery from outside (a switched outlet).

---

## GET /api/health/storage

The read route is on the public allowlist, so a monitoring agent can poll it without a
session even when `[Web_Viewer] web_viewer_password` is set. The payload is aggregate
only: counters, booleans and codes — no mount paths, no configuration values.

The probe write is **not** recorded as a storage fault (`record=False`), so a prober
polling this route every minute cannot push the bot over `failure_threshold`.

| Key | Meaning |
|-----|---------|
| `state` | `ok`, `degraded` (probe failed or monitor disabled) or `fault` (a read-only mount or `EROFS`) |
| `enabled` | `[IO_Health] enabled` |
| `mounts_checked` | number of monitored prefixes |
| `mounts_readonly` | how many of them the kernel reports read-only right now |
| `probe_write_ok` | the probe file inside every prefix was writable |
| `probe_errno` | comma-separated error codes of the failing probes, empty when all wrote |
| `failures_consecutive` | counted `EROFS` failures (from the bot's process, through the shared database) |
| `failures_total` | all counted `EROFS` failures |
| `threshold_reached` | `failures_consecutive` has reached `failure_threshold` |
| `semaphore_pending` | a reboot request is outstanding in shared memory |
| `last_signal_source` | who raised the last signal, e.g. `IOHealthWatchdog` |
| `last_signal_at` | timestamp of that signal |
| `powercycle_configured` | `powercycle_command` is set |
| `uptime_seconds`, `timestamp` | viewer-side values, as on `/api/health` |

```json
{
  "state": "fault",
  "enabled": true,
  "mounts_checked": 3,
  "mounts_readonly": 1,
  "probe_write_ok": false,
  "probe_errno": "EROFS",
  "failures_consecutive": 4,
  "failures_total": 11,
  "threshold_reached": true,
  "semaphore_pending": true,
  "last_signal_source": "IOHealthWatchdog",
  "last_signal_at": "2026-11-03 07:12:44",
  "powercycle_configured": true
}
```

### Text output

`?format=text` answers `text/plain` with one `key=value` line per field, which is what
a shell or `awk` prober needs. Booleans become `1`/`0`, lists become comma lists, and a
value containing whitespace is single-quoted so one field stays on one line.

```text
state=fault
enabled=1
mounts_checked=3
mounts_readonly=1
probe_write_ok=0
probe_errno=EROFS
failures_consecutive=4
failures_total=11
threshold_reached=1
semaphore_pending=1
last_signal_source=IOHealthWatchdog
last_signal_at='2026-11-03 07:12:44'
powercycle_configured=1
uptime_seconds=86400
timestamp=1762156800
```

```bash
state=$(curl -fsS 'http://localhost:8080/api/health/storage?format=text' | awk -F= '/^state=/ {print $2}')
[ "$state" = fault ] && curl -fsS -X POST 'http://localhost:8080/api/health/storage/powercycle'
```

---

## POST /api/health/storage/powercycle

Admin-only (session cookie plus `X-Requested-With`), no extra gate: it runs
`[IO_Health] powercycle_command` as configured.

- **200** when the hook ran, **502** when it failed or timed out — a monitoring script
  can tell "delivered" from "not delivered".
- `powercycle_command` empty (default) raises the shared-memory semaphore instead, which
  is what the external watcher consumes. The response then reports `action=semaphore`.
- The `reason` (JSON body or `?reason=`) is collapsed to a single line before it reaches
  the semaphore info file, so a caller cannot inject extra lines into it.

```json
{
  "powercycle": "ok",
  "action": "command",
  "detail": "plug acknowledged",
  "state": "fault",
  "semaphore_pending": false
}
```

`?format=text` gives the same fields as `key=value` lines.

```bash
curl -fsS -X POST -H 'X-Requested-With: XMLHttpRequest' \
  -H 'Content-Type: application/json' \
  -d '{"reason": "nvme dead"}' \
  'http://localhost:8080/api/health/storage/powercycle?format=text'
```

```ini
[IO_Health]
# Example for a switched outlet: off, wait, on again. The command comes from
# config.ini, so it runs with the privileges of the bot user.
powercycle_command = curl -fsS --max-time 8 'http://plug.local/?cmd=PowerOff' && sleep 12 && curl -fsS --max-time 8 'http://plug.local/?cmd=PowerOn'
powercycle_timeout_seconds = 10
```

---

## Example monitoring agent

Runs from another machine, so it keeps working when this one is unusable:

```bash
#!/bin/sh
text=$(curl -fsS "http://bot.local:8080/api/health/storage?format=text")
state=$(printf '%s\n' "$text" | awk -F= '/^state=/ {print $2}')

if [ "$state" = fault ]; then
    answer=$(curl -fsS -X POST -H 'X-Requested-With: XMLHttpRequest' \
        "http://bot.local:8080/api/health/storage/powercycle?format=text") || exit 1
    printf '%s\n' "$answer" | awk -F= '/^powercycle=/ && $2 == "failed" {exit 1}'
fi
```

See also: [Configuration overview](configuration.md), [Web Viewer](web-viewer.md).
