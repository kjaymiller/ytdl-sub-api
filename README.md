# ytdl-sub-api

An HTTP wrapper around [ytdl-sub](https://github.com/jmbannon/ytdl-sub), bundled
with a cron sidecar, as a single `docker compose` stack.

ytdl-sub itself is a one-shot CLI — no daemon, no HTTP surface. This project
bolts on:

- a small Flask API that CRUDs `subscriptions.yaml` and can trigger an
  on-demand run,
- an [ofelia](https://github.com/mcuadros/ofelia) sidecar that runs
  `ytdl-sub sub` on a cron schedule.

Useful if you want to subscribe to channels from a browser extension, a
home-assistant automation, a script — anything that can make an HTTP call.

## Quick start

```sh
git clone https://github.com/kjaymiller/ytdl-sub-api.git
cd ytdl-sub-api

cp .env.example .env
# Edit .env — at minimum set API_TOKEN.
# `openssl rand -hex 32` generates a good one.

cp subscriptions.example.yaml subscriptions.yaml

docker compose up -d
curl http://localhost:5000/healthz
```

Files are downloaded to `./data/downloads` by default. Override with
`DOWNLOADS_DIR` in `.env` (e.g. a NAS mount).

## API

All endpoints except `/healthz` require `Authorization: Bearer $API_TOKEN`.

| Method | Path                  | Purpose                           |
|--------|-----------------------|-----------------------------------|
| GET    | `/healthz`            | Liveness, no auth.                |
| GET    | `/channels`           | List all subscriptions.           |
| GET    | `/channels?url=<u>`   | Check if a URL is subscribed.     |
| POST   | `/channels`           | Add a subscription.               |
| DELETE | `/channels/<name>`    | Remove a subscription by name.    |
| POST   | `/run`                | Trigger `ytdl-sub sub` now.       |

### POST /channels

```json
{
  "url": "https://www.youtube.com/@exampleChannel",
  "name": "Example Channel",        // optional; derived from URL
  "preset": "Jellyfin TV Show",     // optional; DEFAULT_PRESET
  "keep_days": 14,                  // optional; only_recent_date_range
  "max_files": 10                   // optional; only_recent_max_files
}
```

### URL matching

`/channels?url=` normalizes URLs lightly (lowercase host, strip `www.`, strip
trailing `/videos`, `/featured`, `/streams`, `/playlists`, `/shorts`,
`/community`, `/about`) before comparing. Channels stored under a different
URL form — e.g. `/@handle` vs `/channel/UCxxx` — **will not match**. Pick one
form and stick with it, or extend `_normalize` in `api.py`.

## Configuration

Everything tunable lives in `.env`. See `.env.example` for the full list.

| Var                  | Purpose                                                         |
|----------------------|-----------------------------------------------------------------|
| `API_TOKEN`          | Bearer token. Required.                                         |
| `API_PORT`           | Host port for the API (default `5000`).                         |
| `CRON_SCHEDULE`      | Ofelia 6-field cron (default `0 7 * * * *` — :07 hourly).       |
| `DOWNLOADS_DIR`      | Host dir bind-mounted to `/downloads`.                          |
| `SUBSCRIPTIONS_FILE` | Host path to `subscriptions.yaml`.                              |
| `PUID` / `PGID`      | UID/GID for ytdl-sub container.                                 |
| `TZ`                 | Timezone for cron + logs.                                       |
| `DEFAULT_PRESET`     | Preset used when POST omits one.                                |

## Reverse proxy

The compose publishes port 5000 directly. If you run this behind Traefik,
Caddy, nginx, etc., remove the `ports:` block from `ytdl-sub-api` and route
to the container on port 5000 inside whatever network your proxy shares
with it.

## License

MIT. See `LICENSE`.
