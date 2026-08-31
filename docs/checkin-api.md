# Check-in API contract

The **Check-in service** (local plugin `local/service_plugins/checkin_service.py`) can submit collected check-ins to a web API. This document describes the contract so you can run or build a server that accepts submissions from one or more bots. The bot is the client; the server is not part of this repo.

## Authentication

- **Header**: `Authorization: Bearer <api_key>`
- The bot sends the API key configured in `[CheckIn]` `api_key` or the `CHECKIN_API_KEY` environment variable.
- Server should validate the key (e.g. constant-time comparison against a configured secret) and return **401 Unauthorized** if missing or invalid.
- Use HTTPS in production so the key is not sent in the clear.

## Endpoint

- **Method**: `POST`
- **URL**: Configured by the bot as `[CheckIn]` `api_url` (e.g. `https://example.com/checkins` or `https://example.com/v1/checkins`).
- **Content-Type**: `application/json`

## Request body

Each request is a single check-in. The bot sends one POST per check-in when flushing (e.g. daily at flush time).

| Field          | Type   | Description |
|----------------|--------|-------------|
| `packet_hash`  | string | Unique id for this check-in (from packet or fallback hash). Server should use this for deduplication. |
| `username`     | string | Sender name (from "SENDER: message" on the mesh). |
| `message`      | string | Message content (part after the colon). |
| `channel`      | string | Channel name (e.g. "#meshmonday"). |
| `timestamp`    | string | ISO 8601 datetime when the check-in was received (bot timezone). |
| `source_bot`   | string | Optional. Bot name if configured; useful when multiple bots submit. |

Example:

```json
{
  "packet_hash": "A1B2C3D4E5F67890",
  "username": "HOWL",
  "message": "check in",
  "channel": "#meshmonday",
  "timestamp": "2025-03-03T14:30:00-07:00",
  "source_bot": "meshcore-bot"
}
```

## Deduplication

- **packet_hash** identifies the same logical check-in across bots and retries. Multiple bots that hear the same packet will send the same `packet_hash`; the server should store at most one record per `packet_hash` (e.g. upsert or ignore duplicate).
- The bot sends each check-in once per flush; retries on 5xx/429 may send the same body again, so idempotency by `packet_hash` is required.

## Response

- **200 OK** or **201 Created**: Success. Body is optional.
- **401 Unauthorized**: Invalid or missing API key.
- **4xx/5xx**: Bot may retry once after a short delay (e.g. 5 s) for 429 and 5xx; then it logs and continues. No need to return a specific body for errors.

## Example server (sketch)

A minimal server could:

1. Verify `Authorization: Bearer <secret>` against a configured key.
2. Parse JSON body; validate required fields (`packet_hash`, `username`, `message`, `channel`, `timestamp`).
3. Upsert into a database keyed by `packet_hash` (e.g. SQLite or Postgres).
4. Return 201 with an empty or minimal JSON body.

No reference server is included in this repo; use any stack (e.g. Flask, FastAPI) that supports HTTP and env-based secrets.

## Example receiver (stdlib)

The repo includes a **stdlib-only** reference server you can run behind nginx with no pip or virtualenv.

### Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `CHECKIN_API_SECRET` | Yes | Bearer secret; must match the bot's `[CheckIn]` `api_key` (or `CHECKIN_API_KEY` env). |
| `CHECKIN_PORT` | No | Port to bind (default `9999`). |
| `CHECKIN_DB_PATH` | No | SQLite file path (default `./checkins.db`). Parent directory is created if missing. |

Use HTTPS in production so the key is not sent in the clear. Run the script on `127.0.0.1` and put nginx in front for TLS termination.

### Nginx (minimal)

Proxy a location to the script's port (TLS and server name are configured elsewhere):

```nginx
location /checkins {
    limit_except POST { deny all; }
    client_max_body_size 4k;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_pass http://127.0.0.1:9999;
}
```

Use the same port as `CHECKIN_PORT` (e.g. 9999). The script accepts both `/` and `/checkins` for POST; GET to `/` or `/checkins` returns `{"status":"ok"}` for health checks.

### Systemd

Example unit `/etc/systemd/system/checkin-receiver.service`:

```ini
[Unit]
Description=Check-in API receiver (meshcore-bot)
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /path/to/meshcore-bot/scripts/checkin_receiver.py
Environment=CHECKIN_API_SECRET=your_secret_here
Environment=CHECKIN_PORT=9999
Environment=CHECKIN_DB_PATH=/var/lib/checkin/checkins.db
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Create the database directory and set permissions as needed:

```bash
sudo mkdir -p /var/lib/checkin
sudo chown www-data:www-data /var/lib/checkin   # or the user running the script
```

Reload and enable:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now checkin-receiver
```

Configure the bot's `[CheckIn]` `api_url` to your public URL (e.g. `https://yourdomain.com/checkins`) and set `api_key` (or `CHECKIN_API_KEY`) to the same value as `CHECKIN_API_SECRET` on the server.
