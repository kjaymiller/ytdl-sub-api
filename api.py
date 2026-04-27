"""Tiny CRUD API over ytdl-sub's subscriptions.yaml.

The file is ytdl-sub's native format (a top-level preset key like
"Jellyfin TV Show" mapping display-names to subscription blocks). We
preserve comments and key order via ruamel.yaml so the git-tracked
file stays readable after API writes.

Endpoints (all require `Authorization: Bearer <API_TOKEN>` except
/healthz):
  GET  /healthz
  GET  /presets                           -> base preset + user-defined profiles
  GET  /channels                          -> list all, enriched with on-disk stats
  GET  /channels?url=<youtube_url>        -> 200 with details, or 404
  POST /channels                          -> add
       body: {url, name?, profile?, preset?, keep_days?, max_files?}
  DELETE /channels/<name>                 -> remove
  POST /run                               -> docker exec ytdl-sub to pull now
  GET  /runs?limit=N                      -> recent ofelia run history (default 20)
  GET  /downloads                         -> per-folder snapshot under /downloads

URL matching is done after a light normalization (lowercased host,
www stripped, path suffixes like /videos stripped, query/fragment
stripped). Channels stored under a different URL form (@handle vs
/channel/UCxxx) won't match — document this in the runbook.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import docker
from flask import Flask, jsonify, request
from flask_cors import CORS
from ruamel.yaml import YAML

SUBS_PATH = os.environ.get("SUBS_PATH", "/subs/subscriptions.yaml")
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/config.yaml")
CONTAINER = os.environ.get("YTDL_SUB_CONTAINER", "ytdl-sub")
API_TOKEN = os.environ.get("API_TOKEN", "")
DEFAULT_PRESET = os.environ.get("DEFAULT_PRESET", "Jellyfin TV Show")
# Base preset used when POST /channels passes `profile`. The stored key
# becomes f"{BASE_PRESET} | {profile}", which ytdl-sub resolves as a
# chained preset (e.g. "Jellyfin TV Show by Date | long_collection").
# Defaults to DEFAULT_PRESET so single-preset deployments need no change.
BASE_PRESET = os.environ.get("BASE_PRESET", DEFAULT_PRESET)
# Profile name appended to BASE_PRESET when POST sends `keep_days` /
# `max_files` without an explicit `profile` or `preset`. ytdl-sub ships
# an `Only Recent` preset whose `only_recent_*` overrides drive the
# date-range / max-files filtering, so per-channel manual overrides
# need that preset somewhere in the chain to take effect.
MANUAL_PROFILE = os.environ.get("MANUAL_PROFILE", "Only Recent")
DOWNLOADS_DIR = Path(os.environ.get("DOWNLOADS_VIEW", "/downloads"))
OFELIA_LOGS_DIR = Path(os.environ.get("OFELIA_LOGS_VIEW", "/var/log/ofelia"))
MEDIA_EXTS = {".mp4", ".mkv", ".webm", ".m4a", ".mp3", ".opus", ".ogg", ".flac"}

if not API_TOKEN:
    print("FATAL: API_TOKEN env var must be set", file=sys.stderr)
    sys.exit(1)

yaml = YAML()
yaml.preserve_quotes = True
yaml.indent(mapping=2, sequence=4, offset=2)

app = Flask(__name__)
CORS(app)


def _load() -> dict:
    with open(SUBS_PATH) as f:
        data = yaml.load(f) or {}
    # Migrate legacy entries written by older versions of this API
    # (top-level `url:` inside a sub block — ytdl-sub rejects that
    # field with "Allowed fields: ... overrides, preset, ..." and
    # blocks the cron run on validation). Idempotent; touches only
    # blocks that need it; preserves any other keys.
    _migrate_subs(data)
    return data


def _sub_url(sub) -> str:
    """Extract the URL from a sub block in any of the shapes ytdl-sub
    accepts (or that older versions of this API wrote).

    Order of precedence:
      "Channel": "https://..."                       # scalar
      "Channel": ["https://...", ...]                # list
      "Channel": {overrides: {url: "..."}}           # correct dict shape
      "Channel": {url: "..."}                        # legacy/broken dict shape

    The legacy shape is what `add_channel` produced before this fix —
    `_load` migrates it on read, but `_sub_url` still tolerates it so
    callers don't crash on a partly-migrated file.
    """
    if isinstance(sub, str):
        return sub
    if isinstance(sub, list) and sub:
        return sub[0]
    if isinstance(sub, dict):
        ov = sub.get("overrides") or {}
        if isinstance(ov, dict) and ov.get("url"):
            return ov["url"]
        if sub.get("url"):
            return sub["url"]
    return ""


# ytdl-sub's preset-level schema. If a dict has any of these as direct
# children, it's a subscription block — NOT a preset-name block whose
# children are subscriptions. Source: validation error message
# "Allowed fields: ..." emitted when an unknown field appears.
_PRESET_FIELDS = frozenset({
    "_view", "audio_extract", "chapters", "date_range", "download",
    "embed_thumbnail", "file_convert", "filter_exclude", "filter_include",
    "format", "match_filters", "music_tags", "nfo_tags",
    "output_directory_nfo_tags", "output_options", "overrides", "preset",
    "split_by_chapters", "square_thumbnail", "static_nfo_tags", "subtitles",
    "throttle_protection", "video_tags", "ytdl_options",
})


def _looks_like_sub_block(value) -> bool:
    """True if `value` is a single subscription block (dict with any
    preset-level field as a direct key), not a container of subs.

    Lets us distinguish:
      "Some Preset":            -> container of subs
        "@chan": "url"
      "@chan":                  -> single sub at top level
        preset: [...]
        overrides: {url: ...}
    """
    if not isinstance(value, dict):
        return False
    return any(k in _PRESET_FIELDS for k in value.keys())


def _migrate_subs(data: dict) -> None:
    """Rewrite legacy `{url: ...}` sub blocks into `{overrides: {url: ...}}`.

    Mutates `data` in place. ruamel preserves comments and key order
    around mutated nodes. Safe to call repeatedly.

    Handles two top-level shapes:
      1. preset-name → subs container (subs are nested children)
      2. standalone subscription (preset-level fields directly under
         the top-level key)
    Only Shape 1's children are subject to migration. Shape 2 entries
    already use `overrides.url` correctly — touching them would corrupt
    their structure.
    """
    for top_key, block in data.items():
        if top_key.startswith("__") or not isinstance(block, dict):
            continue
        if _looks_like_sub_block(block):
            # Shape 2 — top-level standalone sub. Already-correct shape
            # (or, if it has a stray `url:` next to `overrides:`, that's
            # the same legacy pattern; migrate the top-level block too).
            _migrate_one(block)
            continue
        # Shape 1 — container of subscriptions.
        for _name, sub in list(block.items()):
            if isinstance(sub, dict):
                _migrate_one(sub)


def _migrate_one(sub: dict) -> None:
    """Move a top-level `url` key inside this sub block into `overrides.url`."""
    url = sub.get("url")
    if not url:
        return
    ov = sub.get("overrides")
    if not isinstance(ov, dict):
        ov = {}
        sub["overrides"] = ov
    # Don't clobber a correctly-shaped overrides.url if both somehow
    # coexist; prefer the new location.
    ov.setdefault("url", url)
    del sub["url"]


def _load_profiles() -> list[str]:
    """User-defined preset names from CONFIG_PATH's `presets:` block.

    These are the profile names callers can pass as `profile` on POST.
    Returns [] if the config file is missing, malformed, or has no
    `presets:` key — the API is still useful with raw `preset:` strings.
    """
    try:
        with open(CONFIG_PATH) as f:
            cfg = yaml.load(f) or {}
    except OSError:
        return []
    presets = cfg.get("presets")
    if not isinstance(presets, dict):
        return []
    return [k for k in presets if isinstance(k, str) and not k.startswith("__")]


def _plain(v):
    """Coerce ruamel YAML containers/scalars into JSON-safe primitives."""
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_plain(x) for x in v]
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)


def _load_profile_details() -> dict[str, dict]:
    """Per-profile parent chain + overrides, for clients that want to
    show what a profile does (date range, max files, etc.) instead of
    just rendering an opaque name. Same failure modes as _load_profiles().
    """
    try:
        with open(CONFIG_PATH) as f:
            cfg = yaml.load(f) or {}
    except OSError:
        return {}
    presets = cfg.get("presets")
    if not isinstance(presets, dict):
        return {}
    out: dict[str, dict] = {}
    for name, body in presets.items():
        if not isinstance(name, str) or name.startswith("__") or not isinstance(body, dict):
            continue
        parents = body.get("preset")
        if isinstance(parents, str):
            parents = [parents]
        elif not isinstance(parents, list):
            parents = []
        overrides = body.get("overrides")
        out[name] = {
            "parents": [str(x) for x in parents],
            "overrides": _plain(overrides) if isinstance(overrides, dict) else {},
        }
    return out


def _save(data: dict) -> None:
    buf = io.StringIO()
    yaml.dump(data, buf)
    with open(SUBS_PATH, "w") as f:
        f.write(buf.getvalue())


_STRIP_SUFFIXES = ("/videos", "/featured", "/streams", "/playlists", "/shorts", "/community", "/about")


def _normalize(url: str) -> str:
    if not url:
        return ""
    p = urlparse(url.strip())
    host = (p.netloc or p.path).lower().removeprefix("www.")
    path = p.path if p.netloc else ""
    path = path.rstrip("/")
    for suf in _STRIP_SUFFIXES:
        if path.endswith(suf):
            path = path[: -len(suf)]
            break
    return urlunparse(("https", host, path, "", "", ""))


def _iter_subs(data: dict):
    """Walk every subscription in `data`, yielding (preset, name, sub_dict).

    The file has two top-level shapes:
      1. "Preset Name": { "@chan": <sub-value>, ... }
         (preset-name container; nested values are subs)
      2. "@chan": { preset: [...], overrides: {url: ...} }
         (standalone subscription at top level — preset chain inline)

    Sub-values themselves come in three shapes:
      "Channel": "https://..."                       # bare URL
      "Channel": ["https://...", "https://..."]      # multi-URL
      "Channel": {overrides: {url: "..."}, ...}      # full preset block

    Normalize everything to (preset_name, sub_name, dict_with_url).
    The yielded `url` is a synthesized convenience field — not what
    gets written back to subscriptions.yaml.
    """
    for top_key, block in data.items():
        if top_key.startswith("__") or not isinstance(block, dict):
            continue
        if _looks_like_sub_block(block):
            # Shape 2: the top-level key IS the sub. Render its preset
            # chain (a list under `preset:`) as a " | "-joined string
            # to match how Shape-1 entries are addressed.
            preset_field = block.get("preset")
            if isinstance(preset_field, list):
                preset_repr = " | ".join(str(p) for p in preset_field)
            elif isinstance(preset_field, str):
                preset_repr = preset_field
            else:
                preset_repr = ""
            yield preset_repr, top_key, {**block, "url": _sub_url(block)}
            continue
        # Shape 1: top-level key is a preset name; iterate its subs.
        for name, sub in block.items():
            url = _sub_url(sub)
            if isinstance(sub, dict):
                yield top_key, name, {**sub, "url": url}
            elif isinstance(sub, str):
                yield top_key, name, {"url": sub}
            elif isinstance(sub, list) and sub:
                yield top_key, name, {"url": sub[0], "additional_urls": sub[1:]}


def _find_by_url(data: dict, url: str):
    target = _normalize(url)
    if not target:
        return None
    for preset, name, sub in _iter_subs(data):
        if _normalize(sub.get("url", "")) == target:
            return {"preset": preset, "name": name, **sub}
    return None


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(s: str) -> str:
    return _SLUG_RE.sub("", s.lower())


def _scan_downloads() -> dict[str, dict]:
    """Snapshot of each top-level dir under DOWNLOADS_DIR.

    Returns {folder_name: {file_count, latest_mtime, latest_file}}.
    Cheap (one scandir + one walk per top-level dir). Returns {} if
    the dir isn't mounted.
    """
    out: dict[str, dict] = {}
    if not DOWNLOADS_DIR.is_dir():
        return out
    try:
        for entry in os.scandir(DOWNLOADS_DIR):
            if not entry.is_dir(follow_symlinks=False):
                continue
            count = 0
            latest_mtime = 0.0
            latest_file = None
            for root, _dirs, files in os.walk(entry.path):
                for f in files:
                    if Path(f).suffix.lower() not in MEDIA_EXTS:
                        continue
                    count += 1
                    fp = os.path.join(root, f)
                    try:
                        m = os.path.getmtime(fp)
                    except OSError:
                        continue
                    if m > latest_mtime:
                        latest_mtime = m
                        latest_file = f
            out[entry.name] = {
                "file_count": count,
                "latest_mtime": int(latest_mtime) if latest_mtime else None,
                "latest_file": latest_file,
            }
    except OSError:
        return out
    return out


def _match_dir(name: str, snapshot: dict[str, dict]) -> dict | None:
    """Best-effort map a YAML key to one of the on-disk folders.

    ytdl-sub renames folders based on yt-dlp metadata (e.g. the YAML
    key '@SimbaThaGod' becomes 'Simba Tha God' on disk), so an exact
    match is the happy path; we fall back to a slugified compare.
    """
    if name in snapshot:
        return snapshot[name]
    slug = _slug(name)
    for folder, stats in snapshot.items():
        if _slug(folder) == slug:
            return stats
    return None


def _read_runs(limit: int) -> list[dict]:
    """Most-recent ofelia executions from save-folder.

    Ofelia writes one JSON file per execution containing exit code,
    timing, and stdout/stderr. File-naming and exact schema vary; we
    treat any *.json under OFELIA_LOGS_DIR as a run record and return
    sorted by file mtime (newest first).
    """
    if not OFELIA_LOGS_DIR.is_dir():
        return []
    files = []
    try:
        for entry in os.scandir(OFELIA_LOGS_DIR):
            if entry.is_file(follow_symlinks=False) and entry.name.endswith(".json"):
                files.append(entry)
    except OSError:
        return []
    files.sort(key=lambda e: e.stat().st_mtime, reverse=True)
    runs: list[dict] = []
    for entry in files[:limit]:
        try:
            with open(entry.path) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        # Pass through the raw record but always include mtime so the
        # caller has a timestamp even if ofelia's schema shifts.
        data["_file"] = entry.name
        data["_mtime"] = int(entry.stat().st_mtime)
        # Trim stdout/stderr to keep responses bounded.
        for k in ("stdout", "stderr", "Output", "output"):
            v = data.get(k)
            if isinstance(v, str) and len(v) > 4000:
                data[k] = v[-4000:]
        runs.append(data)
    return runs


def _auth_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        header = request.headers.get("Authorization", "")
        if header != f"Bearer {API_TOKEN}":
            return jsonify({"error": "unauthorized"}), 401
        return fn(*a, **kw)

    return wrapper


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})


@app.get("/presets")
@_auth_required
def list_presets():
    return jsonify(
        {
            "base_preset": BASE_PRESET,
            "default_preset": DEFAULT_PRESET,
            "manual_profile": MANUAL_PROFILE,
            "profiles": _load_profiles(),
            "profile_details": _load_profile_details(),
        }
    )


@app.get("/channels")
@_auth_required
def list_or_find_channels():
    data = _load()
    snapshot = _scan_downloads()
    url = request.args.get("url")
    if url:
        hit = _find_by_url(data, url)
        if not hit:
            return jsonify({"subscribed": False, "normalized": _normalize(url)}), 404
        hit["downloads"] = _match_dir(hit["name"], snapshot)
        return jsonify({"subscribed": True, **hit})
    channels = []
    for preset, name, sub in _iter_subs(data):
        entry = {"preset": preset, "name": name, **sub}
        entry["downloads"] = _match_dir(name, snapshot)
        channels.append(entry)
    return jsonify({"channels": channels})


@app.post("/channels")
@_auth_required
def add_channel():
    payload = request.get_json(silent=True) or {}
    url = (payload.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url required"}), 400
    profile = (payload.get("profile") or "").strip()
    raw_preset = (payload.get("preset") or "").strip()
    if profile and raw_preset:
        return jsonify({"error": "pass either profile or preset, not both"}), 400

    overrides = {}
    if payload.get("keep_days") is not None:
        overrides["only_recent_date_range"] = f"{int(payload['keep_days'])}days"
    if payload.get("max_files") is not None:
        overrides["only_recent_max_files"] = int(payload["max_files"])

    if profile:
        # `profile` + per-channel overrides is legal: ytdl-sub merges the
        # sub-block's overrides over the chained profile's defaults.
        preset = f"{BASE_PRESET} | {profile}"
    elif raw_preset:
        preset = raw_preset
    elif overrides:
        # Manual overrides need `Only Recent` (or whatever MANUAL_PROFILE
        # points at) somewhere in the chain to actually filter — chain it
        # onto BASE_PRESET so the per-sub overrides take effect.
        preset = f"{BASE_PRESET} | {MANUAL_PROFILE}"
    else:
        preset = DEFAULT_PRESET

    name = payload.get("name") or _normalize(url).rsplit("/", 1)[-1] or url

    data = _load()
    existing = _find_by_url(data, url)
    if existing:
        return jsonify({"error": "already subscribed", "existing": existing}), 409

    if preset not in data or not isinstance(data[preset], dict):
        data[preset] = {}

    # ytdl-sub accepts `url` only inside `overrides:` (it's an override
    # variable that the preset chain references as `{url}`). A
    # top-level `url:` next to `overrides:` fails preset validation
    # with "contains the field 'url' which is not allowed".
    #
    # Two valid shapes, picked based on whether we have other overrides:
    #   - No overrides:  scalar string  ("Name": "https://...")
    #   - Any overrides: dict           ("Name": {overrides: {url: ..., ...}})
    if overrides:
        block = {"overrides": {"url": url, **overrides}}
    else:
        block = url
    data[preset][name] = block
    _save(data)
    response = {"preset": preset, "name": name, "url": url}
    if overrides:
        response["overrides"] = overrides
    return jsonify({"added": response}), 201


@app.delete("/channels/<name>")
@_auth_required
def delete_channel(name: str):
    data = _load()
    for preset, block in data.items():
        # Match any sub shape (scalar URL, list, or dict block) — the
        # writer now produces scalar form when there are no overrides,
        # so the old `isinstance(..., dict)` filter would silently fail
        # to delete those.
        if isinstance(block, dict) and name in block:
            del block[name]
            _save(data)
            return jsonify({"deleted": {"preset": preset, "name": name}})
    return jsonify({"error": "not found"}), 404


@app.post("/run")
@_auth_required
def run_now():
    try:
        client = docker.from_env()
        container = client.containers.get(CONTAINER)
        exit_code, output = container.exec_run(
            ["ytdl-sub", "--config", CONFIG_PATH, "sub", "/config/subscriptions.yaml"],
            demux=False,
        )
    except docker.errors.NotFound:
        return jsonify({"error": f"container {CONTAINER} not running"}), 503
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500
    return jsonify(
        {
            "exit_code": exit_code,
            "output_tail": (output.decode("utf-8", errors="replace")[-4000:] if output else ""),
        }
    )


@app.get("/runs")
@_auth_required
def list_runs():
    try:
        limit = max(1, min(100, int(request.args.get("limit", 20))))
    except (TypeError, ValueError):
        limit = 20
    return jsonify({"runs": _read_runs(limit), "save_folder": str(OFELIA_LOGS_DIR)})


@app.get("/downloads")
@_auth_required
def list_downloads():
    snapshot = _scan_downloads()
    folders = [{"folder": name, **stats} for name, stats in sorted(snapshot.items())]
    return jsonify({"root": str(DOWNLOADS_DIR), "folders": folders})


if __name__ == "__main__":
    print(
        f"ytdl-sub-api: subs={SUBS_PATH} config={CONFIG_PATH} "
        f"container={CONTAINER} downloads={DOWNLOADS_DIR} "
        f"ofelia_logs={OFELIA_LOGS_DIR}",
        file=sys.stderr,
    )
    app.run(host="0.0.0.0", port=int(os.environ.get("FLASK_RUN_PORT", 5000)))
