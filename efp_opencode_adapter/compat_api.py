from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from aiohttp import web

from .agents_md import AGENTS_MD_FILENAME, ensure_default_agents_md, read_agents_md, write_agents_md
from .app_keys import SETTINGS_KEY, SESSION_STORE_KEY, TASK_STORE_KEY
from .index_loader import load_skills_index, read_json_file
from .permission_generator import default_permission_baseline, skill_permission_state

SUPPORTED_SYSTEM_PROMPT_SECTION = "agents"
UNSUPPORTED_SYSTEM_PROMPT_SECTIONS = ["soul", "user", "tools", "memory", "daily_notes"]


def _is_supported_system_prompt_section(name: str) -> bool:
    return name == SUPPORTED_SYSTEM_PROMPT_SECTION


def _unsupported_system_prompt_response(name: str) -> web.Response:
    return web.json_response(
        {
            "error": "OpenCode runtime only supports AGENTS.md",
            "engine": "opencode",
            "runtime_type": "opencode",
            "section": name,
            "supported_sections": [SUPPORTED_SYSTEM_PROMPT_SECTION],
        },
        status=422,
    )


def _agents_metadata() -> dict[str, object]:
    return {
        "enabled": True,
        "editable": True,
        "label": AGENTS_MD_FILENAME,
        "filename": AGENTS_MD_FILENAME,
        "path": AGENTS_MD_FILENAME,
        "can_disable": False,
    }


def _agents_md_config_payload(settings) -> dict:
    ensure_default_agents_md(settings)
    return {
        "engine": "opencode",
        "runtime_type": "opencode",
        "sections": [SUPPORTED_SYSTEM_PROMPT_SECTION],
        "agents": _agents_metadata(),
        "unsupported_sections": UNSUPPORTED_SYSTEM_PROMPT_SECTIONS,
    }

# ... unchanged helper funcs

def _clean_repo_url(url: str | None) -> str | None:
    if not url:
        return url
    raw = str(url).strip()
    if not raw:
        return raw
    if "://" not in raw:
        return re.sub(r"^[^/@:]+@([^:]+):", r"\1:", raw)
    try:
        parts = urlsplit(raw)
    except Exception:
        return re.sub(r"^(https?://)[^/@]+@", r"\1", raw)
    if parts.scheme.lower() in {"http", "https", "ssh"}:
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
    settings = request.app[SETTINGS_KEY]
    data = load_skills_index(settings)
    cfg = read_json_file(settings.opencode_config_path) or {}
    permission = cfg.get("permission") if isinstance(cfg.get("permission"), dict) else default_permission_baseline()
    items = data.get("skills", []) if isinstance(data, dict) else []
    skills = []
    for item in items:
        if not isinstance(item, dict) or not item.get("opencode_name"):
            continue
        state = skill_permission_state(permission, item["opencode_name"])
        supported = bool(item.get("opencode_supported", True))
        callable_flag = state in {"allowed", "ask"} and supported
        blocked_reason = "skill is not supported for OpenCode runtime" if not supported else ("skill denied by current OpenCode permission profile" if state == "denied" else None)
        skills.append({"name": item["opencode_name"], "opencode_name": item["opencode_name"], "efp_name": item.get("efp_name"), "description": item.get("description", ""), "tools": item.get("tools", []), "task_tools": item.get("task_tools", []), "risk_level": item.get("risk_level"), "source_path": item.get("source_path"), "runtime_type": "opencode", "engine": "opencode", "permission_state": state, "callable": callable_flag, "blocked_reason": blocked_reason, "opencode_compatibility": item.get("opencode_compatibility", "prompt_only"), "runtime_equivalence": bool(item.get("runtime_equivalence", True)), "programmatic": bool(item.get("programmatic", False)), "opencode_supported": supported, "compatibility_warnings": item.get("compatibility_warnings", []), "tool_mappings": item.get("tool_mappings", []), "opencode_tools": item.get("opencode_tools", []), "missing_tools": item.get("missing_tools", []), "missing_opencode_tools": item.get("missing_opencode_tools", [])})
    return web.json_response({"skills": skills, "engine": "opencode", "count": len(skills), "warnings": data.get("warnings", []) if isinstance(data, dict) else []})


async def queue_status_handler(request: web.Request) -> web.Response:
    records = request.app[TASK_STORE_KEY].list_all()
    counts = {"accepted": 0, "running": 0, "success": 0, "error": 0, "blocked": 0, "cancelled": 0}
    for rec in records:
        if rec.status in counts:
            counts[rec.status] += 1
    return web.json_response({"status": "ok", "engine": "opencode", "queues": {"default": {"total": len(records), **counts}}, "active_sessions": len(request.app[SESSION_STORE_KEY].list_active())})


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
    settings = request.app[SETTINGS_KEY]
    return web.json_response({**_git_info_for_dir(settings.skills_dir), "engine": "opencode"})


async def system_prompt_config_get_handler(request: web.Request) -> web.Response:
    return web.json_response(_agents_md_config_payload(request.app[SETTINGS_KEY]))


async def system_prompt_config_put_handler(request: web.Request) -> web.Response:
    settings = request.app[SETTINGS_KEY]
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid payload", "engine": "opencode"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"error": "Invalid payload", "engine": "opencode"}, status=400)
    for section in payload:
        if section != SUPPORTED_SYSTEM_PROMPT_SECTION:
            return _unsupported_system_prompt_response(section)
    section_payload = payload.get(SUPPORTED_SYSTEM_PROMPT_SECTION, {})
    if not isinstance(section_payload, dict):
        return web.json_response({"error": "Invalid section payload", "engine": "opencode"}, status=400)
    if "enabled" in section_payload and not isinstance(section_payload["enabled"], bool):
        return web.json_response({"error": "Invalid enabled", "engine": "opencode"}, status=400)
    if section_payload.get("enabled") is False:
        return web.json_response({"error": "OpenCode AGENTS.md cannot be disabled", "engine": "opencode", "runtime_type": "opencode", "section": "agents", "supported_sections": ["agents"]}, status=422)
    ensure_default_agents_md(settings)
    return web.json_response({"status": "ok", "engine": "opencode", "runtime_type": "opencode", "sections": ["agents"], "agents": _agents_metadata()})


async def system_prompt_get_handler(request: web.Request) -> web.Response:
    settings = request.app[SETTINGS_KEY]
    name = request.match_info.get("name", "")
    if not _is_supported_system_prompt_section(name):
        return _unsupported_system_prompt_response(name)
    content = read_agents_md(settings)
    return web.json_response({"enabled": True, "content": content, "engine": "opencode", "runtime_type": "opencode", "section": "agents", "label": AGENTS_MD_FILENAME, "filename": AGENTS_MD_FILENAME, "path": AGENTS_MD_FILENAME, "can_disable": False})


async def system_prompt_put_handler(request: web.Request) -> web.Response:
    settings = request.app[SETTINGS_KEY]
    name = request.match_info.get("name", "")
    if not _is_supported_system_prompt_section(name):
        return _unsupported_system_prompt_response(name)
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid payload", "engine": "opencode"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"error": "Invalid payload", "engine": "opencode"}, status=400)
    if "enabled" in payload and not isinstance(payload["enabled"], bool):
        return web.json_response({"error": "Invalid enabled", "engine": "opencode"}, status=400)
    if "content" in payload and not isinstance(payload["content"], str):
        return web.json_response({"error": "Invalid content", "engine": "opencode"}, status=400)
    if payload.get("enabled") is False:
        return web.json_response({"error": "OpenCode AGENTS.md cannot be disabled", "engine": "opencode", "runtime_type": "opencode", "section": "agents", "supported_sections": ["agents"]}, status=422)
    if "content" in payload:
        write_agents_md(settings, payload["content"])
    else:
        ensure_default_agents_md(settings)
    return web.json_response({"status": "ok", "engine": "opencode", "runtime_type": "opencode", "section": "agents", "enabled": True, "path": AGENTS_MD_FILENAME})
