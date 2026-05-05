from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from aiohttp import web

from .index_loader import load_skills_index
from .permission_generator import default_permission_baseline, skill_permission_state
from .index_loader import read_json_file

ALLOWED_PROMPT_SECTIONS = {"soul", "user", "agents", "tools", "memory", "daily_notes"}


def _default_prompt_config() -> dict[str, dict[str, bool]]:
    return {k: {"enabled": True} for k in sorted(ALLOWED_PROMPT_SECTIONS)}


def _config_path(settings) -> Path:
    return settings.adapter_state_dir / "system_prompt_config.json"


def _prompt_path(settings, name: str) -> Path:
    return settings.adapter_state_dir / "system_prompts" / f"{name}.md"


def _valid_name(name: str) -> bool:
    return name in ALLOWED_PROMPT_SECTIONS


def _load_prompt_config(settings) -> dict[str, dict[str, bool]]:
    data = _default_prompt_config()
    path = _config_path(settings)
    if not path.exists():
        return data
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return data
    if not isinstance(payload, dict):
        return data
    for section, value in payload.items():
        if section in ALLOWED_PROMPT_SECTIONS and isinstance(value, dict) and isinstance(value.get("enabled"), bool):
            data[section]["enabled"] = value["enabled"]
    return data


def _clean_repo_url(url: str | None) -> str | None:
    if not url:
        return url
    raw = str(url).strip()
    if not raw:
        return raw

    # Handle scp-like git remotes such as:
    #   git@github.com:org/repo.git
    # This is not a URL with a scheme, but it still contains a username.
    if "://" not in raw:
        return re.sub(r"^[^/@:]+@([^:]+):", r"\1:", raw)

    try:
        parts = urlsplit(raw)
    except Exception:
        return re.sub(r"^(https?://)[^/@]+@", r"\1", raw)

    if parts.scheme.lower() in {"http", "https", "ssh"}:
        # Drop username/password, port, query and fragment.
        # Keep only scheme + hostname + path.
        host = parts.hostname or ""
        if not host:
            return raw
        return urlunsplit((parts.scheme, host, parts.path, "", ""))

    return raw


def _git_info_for_dir(path: Path) -> dict[str, str | None]:
    commit_id: str | None = None
    repo_url: str | None = None
    if not (path / ".git").exists():
        return {"commit_id": commit_id, "repo_url": repo_url}
    try:
        commit_id = subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True, timeout=5).strip() or None
    except Exception:
        commit_id = None
    try:
        repo_url = subprocess.check_output(["git", "-C", str(path), "remote", "get-url", "origin"], text=True, timeout=5).strip() or None
    except Exception:
        repo_url = None
    return {"commit_id": commit_id, "repo_url": _clean_repo_url(repo_url)}


async def skills_handler(request: web.Request) -> web.Response:
    settings = request.app["settings"]
    data = load_skills_index(settings)
    cfg = read_json_file(settings.opencode_config_path) or {}
    permission = cfg.get("permission") if isinstance(cfg.get("permission"), dict) else default_permission_baseline()
    items = data.get("skills", []) if isinstance(data, dict) else []
    skills = []
    for item in items:
        if not isinstance(item, dict) or not item.get("opencode_name"):
            continue
        state = skill_permission_state(permission, item["opencode_name"])
        skills.append(
            {
                "name": item["opencode_name"],
                "opencode_name": item["opencode_name"],
                "efp_name": item.get("efp_name"),
                "description": item.get("description", ""),
                "tools": item.get("tools", []),
                "task_tools": item.get("task_tools", []),
                "risk_level": item.get("risk_level"),
                "source_path": item.get("source_path"),
                "runtime_type": "opencode",
                "engine": "opencode",
                "permission_state": state,
                "callable": state in {"allowed", "ask"},
                "blocked_reason": "skill denied by current OpenCode permission profile" if state == "denied" else None,
            }
        )
    return web.json_response({"skills": skills, "engine": "opencode", "count": len(skills), "warnings": data.get("warnings", []) if isinstance(data, dict) else []})


async def queue_status_handler(request: web.Request) -> web.Response:
    records = request.app["task_store"].list_all()
    counts = {"accepted": 0, "running": 0, "success": 0, "error": 0, "blocked": 0, "cancelled": 0}
    for rec in records:
        if rec.status in counts:
            counts[rec.status] += 1
    return web.json_response({"status": "ok", "engine": "opencode", "queues": {"default": {"total": len(records), **counts}}, "active_sessions": len(request.app["session_store"].list_active())})


async def git_info_handler(request: web.Request) -> web.Response:
    info = _git_info_for_dir(Path("/app/runtime"))
    if not info["commit_id"] and not info["repo_url"]:
        info = _git_info_for_dir(Path.cwd())
    if not info["commit_id"]:
        cpath = os.environ.get("COMMIT_FILE_PATH")
        if cpath and Path(cpath).exists():
            info["commit_id"] = Path(cpath).read_text(encoding="utf-8").strip() or None
    if not info["repo_url"]:
        rpath = os.environ.get("REPO_URL_FILE_PATH")
        if rpath and Path(rpath).exists():
            info["repo_url"] = _clean_repo_url(Path(rpath).read_text(encoding="utf-8").strip() or None)
    return web.json_response({**info, "engine": "opencode"})


async def skill_git_info_handler(request: web.Request) -> web.Response:
    settings = request.app["settings"]
    return web.json_response({**_git_info_for_dir(settings.skills_dir), "engine": "opencode"})


async def system_prompt_config_get_handler(request: web.Request) -> web.Response:
    return web.json_response(_load_prompt_config(request.app["settings"]))


async def system_prompt_config_put_handler(request: web.Request) -> web.Response:
    settings = request.app["settings"]
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid payload"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"error": "Invalid payload"}, status=400)
    current = _load_prompt_config(settings)
    for section, value in payload.items():
        if section not in ALLOWED_PROMPT_SECTIONS or not isinstance(value, dict):
            return web.json_response({"error": "Invalid section"}, status=400)
        if "enabled" in value and not isinstance(value["enabled"], bool):
            return web.json_response({"error": "Invalid enabled"}, status=400)
        if "enabled" in value:
            current[section]["enabled"] = value["enabled"]
    path = _config_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    return web.json_response({"status": "ok", "engine": "opencode"})


async def system_prompt_get_handler(request: web.Request) -> web.Response:
    settings = request.app["settings"]
    name = request.match_info.get("name", "")
    if not _valid_name(name):
        return web.json_response({"error": "Invalid name"}, status=400)
    config = _load_prompt_config(settings)
    content = ""
    if name != "daily_notes":
        path = _prompt_path(settings, name)
        if path.exists():
            content = path.read_text(encoding="utf-8")
    return web.json_response({"enabled": config[name]["enabled"], "content": content, "engine": "opencode"})


async def system_prompt_put_handler(request: web.Request) -> web.Response:
    settings = request.app["settings"]
    name = request.match_info.get("name", "")
    if not _valid_name(name):
        return web.json_response({"error": "Invalid name"}, status=400)
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid payload"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"error": "Invalid payload"}, status=400)
    if "enabled" in payload and not isinstance(payload["enabled"], bool):
        return web.json_response({"error": "Invalid enabled"}, status=400)
    if "content" in payload and not isinstance(payload["content"], str):
        return web.json_response({"error": "Invalid content"}, status=400)
    if "enabled" in payload:
        current = _load_prompt_config(settings)
        current[name]["enabled"] = payload["enabled"]
        path = _config_path(settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    if name != "daily_notes" and "content" in payload:
        path = _prompt_path(settings, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload["content"], encoding="utf-8")
    return web.json_response({"status": "ok", "engine": "opencode"})
