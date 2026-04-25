"""Tiny CRUD API over ytdl-sub's subscriptions.yaml.

The file is ytdl-sub's native format (a top-level preset key like
"Jellyfin TV Show" mapping display-names to subscription blocks). We
preserve comments and key order via ruamel.yaml so the git-tracked
file stays readable after API writes.

Endpoints (all require `Authorization: Bearer <API_TOKEN>` except
/healthz):
  GET  /healthz
  GET  /channels                          -> list all, enriched with on-disk stats
  GET  /channels?url=<youtube_url>        -> 200 with details, or 404
  POST /channels                          -> add
       body: {url, name?, keep_days?, max_files?, preset?}
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
        return yaml.load(f) or {}


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
    for preset, block in data.items():
        if preset.startswith("__") or not isinstance(block, dict):
            continue
        for name, sub in block.items():
            if isinstance(sub, dict):
                yield preset, name, sub


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
    preset = payload.get("preset") or DEFAULT_PRESET
    name = payload.get("name") or _normalize(url).rsplit("/", 1)[-1] or url

    data = _load()
    existing = _find_by_url(data, url)
    if existing:
        return jsonify({"error": "already subscribed", "existing": existing}), 409

    if preset not in data or not isinstance(data[preset], dict):
        data[preset] = {}

    overrides = {}
    if payload.get("keep_days") is not None:
        overrides["only_recent_date_range"] = f"{int(payload['keep_days'])}days"
    if payload.get("max_files") is not None:
        overrides["only_recent_max_files"] = int(payload["max_files"])

    block = {"url": url}
    if overrides:
        block["overrides"] = overrides
    data[preset][name] = block
    _save(data)
    return jsonify({"added": {"preset": preset, "name": name, **block}}), 201


@app.delete("/channels/<name>")
@_auth_required
def delete_channel(name: str):
    data = _load()
    for preset, block in data.items():
        if isinstance(block, dict) and name in block and isinstance(block[name], dict):
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
        f"ytdl-sub-api: subs={SUBS_PATH} container={CONTAINER} "
        f"downloads={DOWNLOADS_DIR} ofelia_logs={OFELIA_LOGS_DIR}",
        file=sys.stderr,
    )
    app.run(host="0.0.0.0", port=int(os.environ.get("FLASK_RUN_PORT", 5000)))
