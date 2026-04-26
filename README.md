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

| Method | Path                  | Purpose                                            |
|--------|-----------------------|----------------------------------------------------|
| GET    | `/healthz`            | Liveness, no auth.                                 |
| GET    | `/presets`            | Base preset + user-defined profiles from config.   |
| GET    | `/channels`           | List subscriptions, enriched with on-disk stats.   |
| GET    | `/channels?url=<u>`   | Check if a URL is subscribed.                      |
| POST   | `/channels`           | Add a subscription.                                |
| DELETE | `/channels/<name>`    | Remove a subscription by name.                     |
| POST   | `/run`                | Trigger `ytdl-sub sub` now.                        |
| GET    | `/runs?limit=N`       | Recent ofelia run history (default 20, max 100).   |
| GET    | `/downloads`          | Per-folder snapshot of `/downloads`.               |

Each `/channels` entry now includes a `downloads` field with
`{file_count, latest_mtime, latest_file}` if the channel's on-disk
folder can be matched (exact name, falling back to a slugified
compare). It's `null` when no folder matches — usually because
ytdl-sub renamed the folder from yt-dlp metadata; `GET /downloads`
shows everything that's actually on disk.

`/runs` reads ofelia's `save-folder` (set in `compose.yml`) — one
JSON record per scheduled execution with exit code, timing, and
stdout/stderr. Useful for "did the last cron tick succeed?" without
shelling into the cron container.

### POST /channels

```json
{
  "url": "https://www.youtube.com/@exampleChannel",
  "name": "Example Channel",        // optional; derived from URL
  "profile": "long_collection",     // optional; combines with BASE_PRESET
  "preset": "Jellyfin TV Show",     // optional escape hatch; mutually exclusive with profile
  "keep_days": 14,                  // optional; only_recent_date_range
  "max_files": 10                   // optional; only_recent_max_files
}
```

`profile` is the recommended way to pick a retention shape: pass a name
from `GET /presets` (e.g. `"long_collection"`) and the API stores the
subscription under `f"{BASE_PRESET} | {profile}"`, which ytdl-sub
resolves as a chained preset. Define the profiles under `presets:` in
your `config.yaml`. Use `preset` only when you need a literal key that
doesn't fit the `<base> | <profile>` shape.

`profile` and `keep_days`/`max_files` can be combined — ytdl-sub merges
the per-subscription `overrides` block over the chained profile's
defaults, so this is the right way to say "use `long_collection` but
keep 50 episodes for this one channel".

If you send `keep_days`/`max_files` with no `profile` (and no `preset`),
the subscription lands under `f"{BASE_PRESET} | {MANUAL_PROFILE}"`
(default `MANUAL_PROFILE=Only Recent`). The chain is required: ytdl-sub's
`only_recent_*` filtering only kicks in when something in the chain
pulls in the `Only Recent` preset.

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
| `CONFIG_FILE`        | Host path to `config.yaml` (custom presets, working dir).       |
| `PUID` / `PGID`      | UID/GID for ytdl-sub container.                                 |
| `TZ`                 | Timezone for cron + logs.                                       |
| `DEFAULT_PRESET`     | Preset used when POST omits both `profile` and `preset`.        |
| `BASE_PRESET`        | Base for chained presets; combined with `profile` on POST.      |
| `MANUAL_PROFILE`     | Profile chained onto `BASE_PRESET` for ad-hoc `keep_days`/`max_files` (default `Only Recent`). |

## Reverse proxy

The compose publishes port 5000 directly. If you run this behind Traefik,
Caddy, nginx, etc., remove the `ports:` block from `ytdl-sub-api` and route
to the container on port 5000 inside whatever network your proxy shares
with it.

## License

MIT. See `LICENSE`.
