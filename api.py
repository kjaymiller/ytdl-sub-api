"""Tiny CRUD API over ytdl-sub's subscriptions.yaml.

The file is ytdl-sub's native format (a top-level preset key like
"Jellyfin TV Show" mapping display-names to subscription blocks). We
preserve comments and key order via ruamel.yaml so the git-tracked
file stays readable after API writes.

Endpoints (all require `Authorization: Bearer <API_TOKEN>` except
/healthz):
  GET  /healthz
  GET  /channels                          -> list all
  GET  /channels?url=<youtube_url>        -> 200 with details, or 404
  POST /channels                          -> add
       body: {url, name?, keep_days?, max_files?, preset?}
  DELETE /channels/<name>                 -> remove
  POST /run                               -> docker exec ytdl-sub to pull now

URL matching is done after a light normalization (lowercased host,
www stripped, path suffixes like /videos stripped, query/fragment
stripped). Channels stored under a different URL form (@handle vs
/channel/UCxxx) won't match — document this in the runbook.
"""
from __future__ import annotations

import io
import os
import sys
from functools import wraps
from urllib.parse import urlparse, urlunparse

import docker
from flask import Flask, jsonify, request
from flask_cors import CORS
from ruamel.yaml import YAML

SUBS_PATH = os.environ.get("SUBS_PATH", "/subs/subscriptions.yaml")
CONTAINER = os.environ.get("YTDL_SUB_CONTAINER", "ytdl-sub")
API_TOKEN = os.environ.get("API_TOKEN", "")
DEFAULT_PRESET = os.environ.get("DEFAULT_PRESET", "Jellyfin TV Show")

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
    url = request.args.get("url")
    if url:
        hit = _find_by_url(data, url)
        if not hit:
            return jsonify({"subscribed": False, "normalized": _normalize(url)}), 404
        return jsonify({"subscribed": True, **hit})
    return jsonify(
        {
            "channels": [
                {"preset": preset, "name": name, **sub}
                for preset, name, sub in _iter_subs(data)
            ]
        }
    )


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
            ["ytdl-sub", "sub", "/config/subscriptions.yaml"], demux=False
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


if __name__ == "__main__":
    print(f"ytdl-sub-api: subs={SUBS_PATH} container={CONTAINER}", file=sys.stderr)
    app.run(host="0.0.0.0", port=int(os.environ.get("FLASK_RUN_PORT", 5000)))
