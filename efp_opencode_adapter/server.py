from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiohttp import web
from .app_keys import (
    SETTINGS_KEY,
    STATE_PATHS_KEY,
    SESSION_STORE_KEY,
    TASK_STORE_KEY,
    CHATLOG_STORE_KEY,
    CHAT_RUN_STORE_KEY,
    USER_DISPLAY_STORE_KEY,
    USAGE_TRACKER_KEY,
    EVENT_BUS_KEY,
    TASK_BACKGROUND_TASKS_KEY,
    OPENCODE_CLIENT_KEY,
    PORTAL_METADATA_CLIENT_KEY,
    RECOVERY_MANAGER_KEY,
    EVENT_BRIDGE_KEY,
    EVENT_BRIDGE_TASK_KEY,
    OPENCODE_PROCESS_MANAGER_KEY,
    OPENCODE_WATCHDOG_TASK_KEY,
    OPENCODE_BINDING_STORE_KEY,
    REQUEST_BINDING_STORE_KEY,
)

from .capabilities import build_capability_catalog
from .chat_api import chat_handler, chat_stream_handler
from .chat_run_validation import validate_chat_run_against_opencode
from .chatlog_store import ChatLogStore
from .chat_run_store import ChatRunStore
from .active_run_payload import active_run_public_from_store, run_state_diagnostics, synthetic_active_run_from_resolved
from .opencode_client import is_session_missing_error
from .opencode_binding_store import OpenCodeBindingStore
from .opencode_thin_api import register_opencode_thin_routes
from .opencode_run_state import ResolvedOpenCodeRunState, resolve_opencode_run_state
from .compat_api import (
    git_info_handler,
    queue_status_handler,
    skill_git_info_handler,
    skills_handler,
    system_prompt_config_get_handler,
    system_prompt_config_put_handler,
    system_prompt_get_handler,
    system_prompt_put_handler,
)
from .event_bus import EventBus, events_ws_handler
from .event_bridge import OpenCodeEventBridge
from .file_routes import register_file_routes
from .opencode_client import OpenCodeClient
from .permissions_api import permission_respond_handler
from .portal_metadata_client import PortalMetadataClient
from .recovery import RecoveryManager
from .usage_api import usage_handler
from .usage_tracker import UsageTracker
from .agents_md import ensure_default_agents_md
from .atlassian_cli_config import write_atlassian_cli_config
from .opencode_config import build_opencode_config, normalize_opencode_provider_id, write_opencode_config
from .opencode_auth import build_opencode_auth_from_runtime_config
from .path_utils import path_exists
from .profile_store import ProfileOverlay, ProfileOverlayStore, build_profile_status_payload, sanitize_profile_config_for_storage, sanitize_public_secrets
from .runtime_env import build_runtime_env_from_config, read_runtime_env_file, write_runtime_env_file
from .thinking_events import safe_preview, utc_now_iso
from .git_cli_auth import write_git_gh_auth_assets
from .opencode_process import OpenCodeProcessManager
from .session_store import SessionStore
from .skill_sync import sync_runtime_skills
from .task_store import TaskStore
from .tasks_api import cancel_task_handler, cleanup_task_background_tasks, execute_task_handler, get_task_handler
from .request_bindings import RequestBindingStore
from .user_display_store import UserDisplayStore
from .sessions_api import (
    clear_sessions_handler,
    delete_message_from_here_handler,
    delete_session_handler,
    edit_message_async_handler,
    edit_message_handler,
    get_session_handler,
    list_sessions_handler,
    rename_session_handler,
    session_chatlog_handler,
)
from .settings import Settings
from .state import build_state_health_snapshot, ensure_state_dirs
import asyncio


GIT_GH_ENV_KEYS = {
    "GH_TOKEN", "GITHUB_TOKEN", "GITHUB_ACCESS_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
    "GITHUB_API_BASE_URL", "EFP_GITHUB_CONFIG_JSON",
    "GH_HOST", "GH_CONFIG_DIR", "GH_PROMPT_DISABLED", "GH_REPO", "GIT_USERNAME", "GIT_PASSWORD", "GIT_ASKPASS",
    "GIT_TERMINAL_PROMPT", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_EDITOR", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
}
STARTUP_FALLBACK_ENV_KEYS = GIT_GH_ENV_KEYS | {"ATLASSIAN_CONFIG"}


def _merge_startup_env_with_process_fallback(settings: Settings, env: dict[str, str]) -> dict[str, str]:
    fallback = build_runtime_env_from_config(settings, {}).env
    merged = dict(env)
    for key in STARTUP_FALLBACK_ENV_KEYS:
        if not merged.get(key) and fallback.get(key):
            merged[key] = fallback[key]
    return merged


def _runtime_env_for_status(settings: Settings, overlay) -> dict[str, str]:
    env_path = Path(overlay.env_path) if overlay and overlay.env_path else settings.adapter_state_dir / "opencode.env"
    if path_exists(env_path):
        return _merge_startup_env_with_process_fallback(settings, read_runtime_env_file(env_path))
    return build_runtime_env_from_config(settings, {}).env


def _active_chat_runs(app: web.Application) -> list:
    store = app.get(CHAT_RUN_STORE_KEY)
    if store is None:
        return []
    if hasattr(store, "list_active"):
        try:
            return list(store.list_active())
        except Exception:
            return []
    runs = getattr(store, "_runs", {})
    if isinstance(runs, dict):
        return [record for record in runs.values() if getattr(record, "status", "") in {"accepted", "running", "recovering", "stream_attached", "stream_detached"}]
    return []


def _is_atlassian_only_profile_change(runtime_config: dict, updated_sections: list[str]) -> bool:
    if not isinstance(runtime_config, dict) or not runtime_config:
        return False
    meaningful_keys = {str(key) for key, value in runtime_config.items() if value not in ({}, [], None, "")}
    if not meaningful_keys or not meaningful_keys.issubset({"jira", "confluence", "atlassian"}):
        return False
    return "atlassian" in set(updated_sections)


async def _publish_restart_deferred(app: web.Application, *, active_runs: list, reason: str) -> None:
    bus = app.get(EVENT_BUS_KEY)
    if bus is None:
        return
    event = {
        "type": "opencode.restart_deferred",
        "event_type": "opencode.restart_deferred",
        "engine": "opencode",
        "state": "pending_restart",
        "summary": "Managed OpenCode restart deferred while chat runs are active.",
        "data": {
            "reason": reason,
            "active_run_count": len(active_runs),
            "active_request_ids": [str(getattr(run, "request_id", "")) for run in active_runs if getattr(run, "request_id", "")],
        },
        "created_at": utc_now_iso(),
        "ts": time.time(),
    }
    await bus.publish(event)


async def health_handler(request: web.Request) -> web.Response:
    settings: Settings = request.app[SETTINGS_KEY]
    client: OpenCodeClient = request.app[OPENCODE_CLIENT_KEY]
    try:
        info = await client.health()
    except Exception:
        info = {"healthy": False, "error": "unavailable"}
    opencode_healthy = bool(info.get("healthy"))
    observed_opencode_version = info.get("version") if opencode_healthy else None
    state_health = build_state_health_snapshot(settings, request.app[STATE_PATHS_KEY])
    state_healthy = bool(state_health.get("healthy"))
    bridge = request.app.get(EVENT_BRIDGE_KEY)
    event_bridge_status = bridge.status_snapshot() if bridge and hasattr(bridge, "status_snapshot") else {"enabled": False, "running": False}
    healthy = opencode_healthy and state_healthy
    payload = {
        "status": "ok" if healthy else "degraded",
        "service": "efp-opencode-runtime",
        "engine": "opencode",
        "opencode_version": observed_opencode_version or settings.opencode_version,
        "opencode": {"healthy": opencode_healthy},
        "state": state_health,
        "event_bridge": event_bridge_status,
        "profile": {k: v for k, v in build_profile_status_payload(settings).items() if k in {"status", "pending_restart", "runtime_profile_id", "revision"}},
    }
    if opencode_healthy:
        payload["opencode"]["version"] = info.get("version")
    else:
        error = sanitize_public_secrets(str(info.get("error", "unavailable")))
        payload["opencode"]["error"] = error if isinstance(error, str) else "unavailable"
    return web.json_response(payload, status=200 if healthy else 503)


async def runtime_profile_apply_handler(request: web.Request) -> web.Response:
    if request.headers.get("X-Portal-Author-Source") != "portal":
        return web.json_response({"success": False, "error": "forbidden", "engine": "opencode"}, status=403)
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "invalid json", "engine": "opencode"}, status=400)
    if not isinstance(payload, dict) or not isinstance(payload.get("config"), dict):
        return web.json_response({"success": False, "error": "config must be an object", "engine": "opencode"}, status=400)
    settings: Settings = request.app[SETTINGS_KEY]
    client = request.app[OPENCODE_CLIENT_KEY]
    runtime_config = payload["config"]
    runtime_profile_id = payload.get("runtime_profile_id")
    revision = payload.get("revision")
    ensure_default_agents_md(settings)
    try:
        sync_runtime_skills(settings)
    except Exception as exc:
        detail = sanitize_public_secrets(str(exc))
        if not isinstance(detail, str):
            detail = str(detail)
        return web.json_response(
            {
                "success": False,
                "engine": "opencode",
                "status": "failed",
                "applied": False,
                "error": "skill_sync_failed",
                "detail": detail,
                "status_endpoint": "/api/internal/runtime-profile/status",
            },
            status=500,
        )
    generated_config, config_hash, updated_sections = build_opencode_config(settings, runtime_config)
    warnings: list[str] = []
    status = "failed"
    applied = False
    pending_restart = False
    config_written = False
    last_error = None
    try:
        write_opencode_config(settings, generated_config)
        config_written = True
    except Exception:
        last_error = "config_write_failed"
        ProfileOverlayStore(settings).save(ProfileOverlay(runtime_profile_id=runtime_profile_id, revision=revision, config=sanitize_profile_config_for_storage(runtime_config), applied_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), generated_config_hash=config_hash, status="failed", pending_restart=False, warnings=warnings, updated_sections=updated_sections, last_apply_error=last_error, applied=False))
        return web.json_response({"success": False, "engine": "opencode", "status": "failed", "applied": False, "pending_restart": False, "config_written": False, "error": "config_write_failed", "warnings": warnings, "status_endpoint": "/api/internal/runtime-profile/status"}, status=500)
    llm = runtime_config.get("llm") if isinstance(runtime_config.get("llm"), dict) else {}
    if llm and any(key in llm for key in ("provider", "model", "api_key", "oauth", "oauth_by_runtime", "temperature", "max_tokens")) and "llm" not in updated_sections:
        updated_sections.append("llm")
    auth_build = build_opencode_auth_from_runtime_config(runtime_config)
    provider = auth_build.provider
    auth_update_status = "skipped"
    if auth_build.warning:
        warnings.append(auth_build.warning)
    if provider and auth_build.auth_info:
        if hasattr(client, "put_auth_info"):
            try:
                auth_result = await client.put_auth_info(provider, auth_build.auth_info)
            except Exception:
                auth_result = {"success": False, "error": "auth update failed"}
        elif hasattr(client, "put_auth") and auth_build.auth_info.get("type") == "api":
            try:
                auth_result = await client.put_auth(provider, auth_build.auth_info.get("key"))
            except Exception:
                auth_result = {"success": False, "error": "auth update failed"}
        else:
            auth_result = {"success": False, "skipped": True}
            warnings.append("opencode auth update skipped")
        if auth_result.get("success"):
            auth_update_status = "updated"
        elif auth_result.get("skipped"):
            auth_update_status = "skipped"
        else:
            warnings.append("opencode auth update failed; manual auth or restart may be required")
            auth_update_status = "failed"
    atlassian_result = write_atlassian_cli_config(settings, runtime_config)
    warnings.extend([item for item in atlassian_result.warnings if item not in warnings])
    if atlassian_result.configured and "atlassian" not in updated_sections:
        updated_sections.append("atlassian")
    env_result = build_runtime_env_from_config(settings, runtime_config)
    runtime_env_has_values = bool(env_result.env)
    env_result.env.update(atlassian_result.env)
    warnings.extend([item for item in env_result.warnings if item not in warnings])
    env_path = write_runtime_env_file(settings, env_result.env)
    git_auth_result = write_git_gh_auth_assets(settings, env_result.env)
    combined_updated_sections = sorted(set(updated_sections + env_result.updated_sections + atlassian_result.updated_sections))
    manager = request.app.get(OPENCODE_PROCESS_MANAGER_KEY)
    restart_performed = False
    health_ok = None
    opencode_pid = None
    restart_meta = {}
    restart_deferred_reason = None
    if manager:
        active_runs = _active_chat_runs(request.app)
        if _is_atlassian_only_profile_change(runtime_config, combined_updated_sections):
            status, applied = "applied", True
            pending_restart = False
        elif active_runs:
            restart_deferred_reason = "active_chat_run"
            pending_restart = True
            status, applied = "pending_restart", False
            await _publish_restart_deferred(request.app, active_runs=active_runs, reason=restart_deferred_reason)
        else:
            # Empty env_result.env should not clear the cached managed env. Use None to preserve
            # the last successful startup/runtime-profile env for OpenCode recovery/watchdog restarts.
            # Atlassian config always contributes ATLASSIAN_CONFIG, so guard on the pre-merge env.
            restart_env = env_result.env if runtime_env_has_values else None
            restart_meta = await manager.restart(restart_env, reason="runtime_profile_apply")
            restart_performed = True
            health_ok = bool(restart_meta.get("health_ok"))
            opencode_pid = restart_meta.get("pid")
            pending_restart = not health_ok
            if pending_restart:
                status, applied, last_error = "failed", False, "opencode_restart_failed"
            else:
                status, applied = "applied", True
    else:
        patch_result: dict = {"success": False, "pending_restart": True}
        if hasattr(client, "patch_config"):
            try:
                patch_result = await client.patch_config(generated_config)
            except Exception:
                patch_result = {"success": False, "pending_restart": True}
            pending_restart = bool(patch_result.get("pending_restart", not patch_result.get("success", False)))
        if pending_restart:
            warnings.append("opencode config patch unsupported; restart may be required")
        status, applied = ("pending_restart", False) if pending_restart else ("applied", True)
    overlay = ProfileOverlay(runtime_profile_id=runtime_profile_id, revision=revision, config=sanitize_profile_config_for_storage(runtime_config), applied_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), generated_config_hash=config_hash, status=status, pending_restart=pending_restart, warnings=warnings, updated_sections=combined_updated_sections, last_apply_error=last_error, applied=applied, env_hash=env_result.env_hash, env_path=str(env_path), restart_performed=restart_performed, opencode_pid=opencode_pid, last_restart_at=restart_meta.get("last_restart_at") if restart_meta else None, last_restart_reason=restart_meta.get("last_restart_reason") if restart_meta else None, health_ok=health_ok, git_auth_configured=bool(git_auth_result.get("configured")), gh_host=git_auth_result.get("host"), gh_config_dir=git_auth_result.get("gh_config_dir"), git_askpass_path=git_auth_result.get("askpass_path"), gitconfig_path=git_auth_result.get("gitconfig_path"), atlassian_cli_configured=atlassian_result.configured, atlassian_config_path=atlassian_result.path, atlassian_jira_instances=atlassian_result.jira_instances, atlassian_confluence_instances=atlassian_result.confluence_instances)
    ProfileOverlayStore(settings).save(overlay)
    response = {"success": True, "engine": "opencode", "runtime_profile_id": runtime_profile_id, "revision": revision, "status": status, "applied": applied, "config_written": config_written, "env_written": True, "env_hash": env_result.env_hash, "env_path": str(env_path), "updated_sections": combined_updated_sections, "config_hash": config_hash, "pending_restart": pending_restart, "warnings": warnings, "auth_update_status": auth_update_status, "auth_provider": auth_build.provider, "auth_type": auth_build.auth_type, "restart_performed": restart_performed, "opencode_pid": opencode_pid, "health_ok": health_ok, "git_auth_configured": bool(git_auth_result.get("configured")), "gh_host": git_auth_result.get("host"), "gh_config_dir": git_auth_result.get("gh_config_dir"), "git_askpass_path": git_auth_result.get("askpass_path"), "gitconfig_path": git_auth_result.get("gitconfig_path"), "atlassian_cli_configured": atlassian_result.configured, "atlassian_config_path": atlassian_result.path, "atlassian_jira_instances": atlassian_result.jira_instances, "atlassian_confluence_instances": atlassian_result.confluence_instances, "atlassian_status": atlassian_result.redacted_status, "status_endpoint": "/api/internal/runtime-profile/status"}
    if restart_deferred_reason:
        response["restart_deferred_reason"] = restart_deferred_reason
    if auth_build.warning:
        response["auth_warning"] = auth_build.warning
    return web.json_response(response, status=500 if manager and pending_restart and last_error == "opencode_restart_failed" else 200)


async def runtime_profile_status_handler(request: web.Request) -> web.Response:
    settings: Settings = request.app[SETTINGS_KEY]
    payload = build_profile_status_payload(settings)

    overlay = ProfileOverlayStore(settings).load()
    if not overlay:
        runtime_env = _runtime_env_for_status(settings, overlay)
        git_askpass_path = runtime_env.get("GIT_ASKPASS")
        gitconfig_path = runtime_env.get("GIT_CONFIG_GLOBAL")
        gh_host = runtime_env.get("GH_HOST") or "github.com"
        env_token_present = bool(
            runtime_env.get("GH_TOKEN")
            or runtime_env.get("GITHUB_TOKEN")
            or runtime_env.get("GH_ENTERPRISE_TOKEN")
            or runtime_env.get("GITHUB_ENTERPRISE_TOKEN")
        )
        payload.update({
            "git_auth_configured": bool(
                env_token_present
                and git_askpass_path
                and gitconfig_path
                and path_exists(Path(git_askpass_path))
                and path_exists(Path(gitconfig_path))
            ),
            "gh_host": gh_host,
            "gh_config_dir": runtime_env.get("GH_CONFIG_DIR"),
            "git_askpass_path": git_askpass_path,
            "gitconfig_path": gitconfig_path,
        })

    return web.json_response(payload)


async def effective_config_handler(request: web.Request) -> web.Response:
    settings: Settings = request.app[SETTINGS_KEY]
    cfg = {}
    if path_exists(settings.opencode_config_path):
        try:
            import json as _json
            cfg = _json.loads(settings.opencode_config_path.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    model = ((cfg.get("agent") or {}).get("efp-main") or {}).get("model")
    provider = normalize_opencode_provider_id(model.split("/", 1)[0] if isinstance(model, str) and "/" in model else "")
    auth_path = settings.opencode_data_dir / "auth.json"
    auth = {}
    if path_exists(auth_path):
        try:
            import json as _json
            auth = _json.loads(auth_path.read_text(encoding="utf-8"))
        except Exception:
            auth = {}
    auth_obj = auth.get(provider) if isinstance(auth, dict) else None
    overlay = ProfileOverlayStore(settings).load()
    profile_cfg = overlay.config if overlay else {}
    github_cfg = profile_cfg.get("github") if isinstance(profile_cfg.get("github"), dict) else {}
    proxy_cfg = profile_cfg.get("proxy") if isinstance(profile_cfg.get("proxy"), dict) else {}
    runtime_env = _runtime_env_for_status(settings, overlay)
    env_token_present = bool(runtime_env.get("GH_TOKEN") or runtime_env.get("GITHUB_TOKEN") or runtime_env.get("GH_ENTERPRISE_TOKEN") or runtime_env.get("GITHUB_ENTERPRISE_TOKEN"))
    config_token_present = bool(github_cfg.get("api_token") or github_cfg.get("token") or github_cfg.get("access_token"))
    gh_host = github_cfg.get("host") or runtime_env.get("GH_HOST") or "github.com"
    git_askpass_path = runtime_env.get("GIT_ASKPASS")
    gitconfig_path = runtime_env.get("GIT_CONFIG_GLOBAL")
    git_askpass_present = bool(git_askpass_path and path_exists(Path(git_askpass_path)))
    gitconfig_present = bool(gitconfig_path and path_exists(Path(gitconfig_path)))
    git_auth_configured = bool(env_token_present and git_askpass_present and gitconfig_present)
    return web.json_response(
        {
            "engine": "opencode",
            "opencode_version": settings.opencode_version,
            "model": model,
            "provider": provider or None,
            "auth": {"provider": provider or None, "present": isinstance(auth_obj, dict), "type": auth_obj.get("type") if isinstance(auth_obj, dict) else None},
            "provider_options": (((cfg.get("provider") or {}).get(provider) or {}).get("options") if provider else {}) or {},
            "config_path": str(settings.opencode_config_path),
            "profile": {
                "runtime_profile_id": overlay.runtime_profile_id if overlay else None,
                "revision": overlay.revision if overlay else None,
            },
            "runtime_integrations": {
                "github": {"enabled": bool(github_cfg) or env_token_present, "base_url": github_cfg.get("api_base_url") or runtime_env.get("GITHUB_API_BASE_URL") or "https://api.github.com", "host": gh_host, "token_present": config_token_present or env_token_present, "git_auth_configured": git_auth_configured, "gh_config_dir": runtime_env.get("GH_CONFIG_DIR"), "git_askpass_present": git_askpass_present, "gitconfig_present": gitconfig_present},
                "proxy": {"enabled": bool(proxy_cfg.get("enabled")), "url_present": bool(proxy_cfg.get("url")), "password_present": bool(proxy_cfg.get("password"))},
                "env_file": {"present": bool(overlay and overlay.env_path), "path": overlay.env_path if overlay else None, "hash": overlay.env_hash if overlay else None},
            },
        }
    )


async def capabilities_handler(request: web.Request) -> web.Response:
    try:
        payload = await build_capability_catalog(request.app[SETTINGS_KEY], request.app[OPENCODE_CLIENT_KEY])
        return web.json_response(payload)
    except Exception:
        return web.json_response({"error": "capabilities unavailable", "engine": "opencode"}, status=500)


async def get_chat_run_handler(request: web.Request) -> web.Response:
    store = request.app[CHAT_RUN_STORE_KEY]
    client = request.app[OPENCODE_CLIENT_KEY]
    request_id = request.match_info["request_id"]
    record = store.get(request_id)
    if record is None:
        return web.json_response({"success": False, "engine": "opencode", "error": "chat_run_not_found"}, status=404)
    validate = str(request.query.get("validate", "")).lower()
    active = str(request.query.get("active", "")).lower() in {"1", "true", "yes", "on"}
    if validate == "opencode" or active:
        public = await validate_chat_run_against_opencode(store=store, client=client, record=record, event_bus=request.app.get(EVENT_BUS_KEY))
    else:
        public = store.to_public_dict(record)
    return web.json_response({"success": True, "engine": "opencode", "run": public})


async def list_chat_runs_handler(request: web.Request) -> web.Response:
    store = request.app[CHAT_RUN_STORE_KEY]
    client = request.app[OPENCODE_CLIENT_KEY]
    session_id = request.query.get("session_id", "")
    if not session_id:
        return web.json_response({"success": False, "engine": "opencode", "error": "session_id_required"}, status=400)
    active = str(request.query.get("active", "")).lower() in {"1", "true", "yes", "on"}
    if active:
        run = store.active_for_session(session_id)
        public = await validate_chat_run_against_opencode(store=store, client=client, record=run, event_bus=request.app.get(EVENT_BUS_KEY))
        runs = [public] if public is not None and public.get("opencode_active") is True else []
    else:
        try:
            limit = int(request.query.get("limit", "20"))
        except ValueError:
            limit = 20
        runs = [store.to_public_dict(record) for record in store.list_for_session(session_id, limit=limit)]
    return web.json_response({"success": True, "engine": "opencode", "runs": [run for run in runs if run is not None]})


def _inactive_resolved_state(
    *,
    opencode_session_id: str = "",
    status: str = "unknown",
    reason: str = "missing_session_binding",
    exists: bool = False,
    diagnostics: dict[str, Any] | None = None,
) -> ResolvedOpenCodeRunState:
    return ResolvedOpenCodeRunState(
        opencode_session_id=opencode_session_id,
        exists=exists,
        active=False,
        status=status,
        source="opencode",
        has_final_assistant=False,
        reason=reason,
        diagnostics=diagnostics or {reason: True},
    )


async def _resolve_portal_session_opencode_state(
    request: web.Request,
    portal_session_id: str,
) -> dict[str, Any]:
    session_store = request.app.get(SESSION_STORE_KEY)
    session_record = session_store.get(portal_session_id) if session_store is not None and hasattr(session_store, "get") else None
    if session_record is None or getattr(session_record, "deleted", False):
        resolved = _inactive_resolved_state(reason="missing_session_binding")
        return {
            "session_record": session_record,
            "opencode_session_id": "",
            "resolved": resolved,
            "exists": False,
            "active": False,
            "status_type": resolved.status or "unknown",
        }

    opencode_session_id = str(getattr(session_record, "opencode_session_id", "") or "")
    if not opencode_session_id:
        resolved = _inactive_resolved_state(
            opencode_session_id="",
            status="missing",
            reason="missing_opencode_session_id",
            diagnostics={"missing_opencode_session_id": True},
        )
    else:
        resolved = await resolve_opencode_run_state(request.app[OPENCODE_CLIENT_KEY], opencode_session_id)
    return {
        "session_record": session_record,
        "opencode_session_id": opencode_session_id,
        "resolved": resolved,
        "exists": bool(resolved.exists),
        "active": bool(resolved.active),
        "status_type": resolved.status or "unknown",
    }


async def _wait_until_opencode_inactive(
    client,
    opencode_session_id: str,
    *,
    timeout_seconds: float = 5.0,
    interval_seconds: float = 0.25,
) -> ResolvedOpenCodeRunState:
    session_id = str(opencode_session_id or "")
    if not session_id:
        return _inactive_resolved_state(status="missing", reason="missing_opencode_session_id")

    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    last_resolved: ResolvedOpenCodeRunState | None = None
    while True:
        try:
            last_resolved = await resolve_opencode_run_state(client, session_id)
        except Exception as exc:
            if is_session_missing_error(exc):
                return _inactive_resolved_state(
                    opencode_session_id=session_id,
                    status="missing",
                    reason="opencode_session_missing",
                    diagnostics={"opencode_status": getattr(exc, "status", None), "status_error": safe_preview(str(exc), 1000)},
                )
            raise
        if not last_resolved.active:
            return last_resolved
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return last_resolved
        await asyncio.sleep(min(max(0.0, float(interval_seconds)), remaining))


def _safe_public_run(store, record) -> dict[str, Any] | None:
    if store is None or record is None or not hasattr(store, "to_public_dict"):
        return None
    try:
        return store.to_public_dict(record)
    except Exception:
        return None


def _chat_run_state_diagnostics(
    store,
    resolved: ResolvedOpenCodeRunState,
    *,
    active_record=None,
    latest_record=None,
    active_public: dict[str, Any] | None = None,
    stale_runs: list | None = None,
) -> dict[str, Any]:
    diagnostics = run_state_diagnostics(resolved)
    if active_record is not None:
        diagnostics["chat_run_store_active"] = active_public or _safe_public_run(store, active_record)
    if latest_record is not None:
        diagnostics["chat_run_store_latest"] = _safe_public_run(store, latest_record)
    if stale_runs is not None:
        stale_count = len(stale_runs)
        if stale_count == 0 and latest_record is not None and getattr(latest_record, "status", "") == "stale":
            stale_count = 1
        diagnostics["chat_run_store_stale_count"] = stale_count
        if stale_runs:
            diagnostics["chat_run_store_stale_because_opencode_idle"] = not resolved.active
            diagnostics["chat_run_store_stale_request_ids"] = [
                str(getattr(record, "request_id", ""))
                for record in stale_runs
                if getattr(record, "request_id", "")
            ]
    return safe_preview(diagnostics, 8000)


def _mark_session_runs_stale_from_opencode(
    store,
    portal_session_id: str,
    reason: str,
    *,
    resolved: ResolvedOpenCodeRunState,
    metadata: dict[str, Any] | None = None,
) -> list:
    if store is None or not hasattr(store, "mark_stale_for_session"):
        return []
    return store.mark_stale_for_session(
        portal_session_id,
        reason,
        metadata={
            "source_of_truth": "opencode",
            "validation_reason": resolved.reason,
            "opencode_active": bool(resolved.active),
            "opencode_status": resolved.status,
            "opencode_exists": bool(resolved.exists),
            "diagnostics": run_state_diagnostics(resolved),
            **(metadata or {}),
        },
    )


def _mark_session_runs_aborted_or_stale(
    store,
    portal_session_id: str,
    reason: str,
    *,
    abort_result: dict[str, Any],
    resolved: ResolvedOpenCodeRunState,
    aborted: bool,
) -> list:
    if store is None or not hasattr(store, "list_for_session"):
        return []
    terminal = {"completed", "incomplete", "failed", "cancelled", "aborted", "stale"}
    updated = []
    for record in list(store.list_for_session(portal_session_id, limit=1000)):
        if getattr(record, "status", "") in terminal:
            continue
        metadata = {
            "abort_result": safe_preview(abort_result, 2000),
            "source_of_truth": "opencode",
            "opencode_active": bool(resolved.active),
            "opencode_status": resolved.status,
            "opencode_exists": bool(resolved.exists),
            "diagnostics": run_state_diagnostics(resolved),
        }
        if aborted and hasattr(store, "mark_aborted"):
            current = store.mark_aborted(record.request_id, reason=reason, metadata=metadata)
        elif hasattr(store, "mark_stale"):
            current = store.mark_stale(record.request_id, reason=reason, metadata=metadata)
        else:
            current = None
        if current is not None:
            updated.append(current)
    return updated


def _extract_created_opencode_session_id(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    for key in ("id", "session_id", "sessionID", "uuid"):
        item = value.get(key)
        if item:
            return str(item)
    for key in ("session", "data", "info"):
        nested = value.get(key)
        if isinstance(nested, dict):
            nested_id = _extract_created_opencode_session_id(nested)
            if nested_id:
                return nested_id
    return ""


async def _publish_session_event(
    request: web.Request,
    event_type: str,
    *,
    portal_session_id: str,
    old_opencode_session_id: str = "",
    opencode_session_id: str = "",
    state: str = "completed",
    summary: str = "",
    data: dict[str, Any] | None = None,
) -> None:
    bus = request.app.get(EVENT_BUS_KEY)
    if bus is None or not hasattr(bus, "publish"):
        return
    await bus.publish(
        {
            "type": event_type,
            "event_type": event_type,
            "engine": "opencode",
            "session_id": portal_session_id,
            "request_id": "",
            "opencode_session_id": opencode_session_id,
            "old_opencode_session_id": old_opencode_session_id,
            "state": state,
            "summary": summary or event_type,
            "data": safe_preview(data or {}, 2000),
            "created_at": utc_now_iso(),
            "ts": time.time(),
        }
    )


async def _hard_reset_portal_session_binding(
    request: web.Request,
    *,
    portal_session_id: str,
    old_opencode_session_id: str,
    abort_result: dict[str, Any] | None = None,
    old_resolved: ResolvedOpenCodeRunState | None = None,
    reason: str = "opencode_abort_still_active_detached",
) -> dict[str, Any]:
    session_store = request.app.get(SESSION_STORE_KEY)
    store = request.app.get(CHAT_RUN_STORE_KEY)
    client = request.app[OPENCODE_CLIENT_KEY]
    session_record = session_store.get(portal_session_id) if session_store is not None and hasattr(session_store, "get") else None
    if session_store is None or session_record is None or getattr(session_record, "deleted", False):
        raise RuntimeError("missing_session_binding")
    new_session = await client.create_session(title=getattr(session_record, "title", "") or "Chat")
    new_opencode_session_id = _extract_created_opencode_session_id(new_session)
    if not new_opencode_session_id:
        raise RuntimeError("opencode_create_session_missing_id")
    session_store.replace_opencode_session_after_mutation(
        portal_session_id,
        new_opencode_session_id,
        message_count=0,
        last_message="",
    )
    stale_runs = []
    if store is not None and hasattr(store, "mark_stale_for_session"):
        stale_runs = store.mark_stale_for_session(
            portal_session_id,
            reason,
            metadata={
                "source_of_truth": "opencode",
                "old_opencode_session_id": old_opencode_session_id,
                "new_opencode_session_id": new_opencode_session_id,
                "abort_result": safe_preview(abort_result or {}, 2000),
                "old_resolved": run_state_diagnostics(old_resolved) if old_resolved is not None else {},
            },
        )
    await _publish_session_event(
        request,
        "opencode.session.detached_after_abort",
        portal_session_id=portal_session_id,
        old_opencode_session_id=old_opencode_session_id,
        opencode_session_id=new_opencode_session_id,
        state="detached",
        summary="Portal session rebound to a fresh OpenCode session.",
        data={
            "reason": reason,
            "abort_result": safe_preview(abort_result or {}, 2000),
            "stale_count": len(stale_runs),
        },
    )
    return {
        "new_opencode_session_id": new_opencode_session_id,
        "new_session": safe_preview(new_session, 2000),
        "stale_runs": stale_runs,
    }


async def active_chat_run_for_session_handler(request: web.Request) -> web.Response:
    store = request.app[CHAT_RUN_STORE_KEY]
    session_id = request.match_info["session_id"]
    state = await _resolve_portal_session_opencode_state(request, session_id)
    opencode_session_id = state["opencode_session_id"]
    resolved = state["resolved"]

    active_record = store.active_for_session(session_id)
    latest_record = store.latest_for_session(session_id) if hasattr(store, "latest_for_session") else None
    if not resolved.active:
        stale_runs = _mark_session_runs_stale_from_opencode(
            store,
            session_id,
            "chat_run_store_stale_because_opencode_idle",
            resolved=resolved,
        )
        return web.json_response(
            {
                "success": True,
                "engine": "opencode",
                "source_of_truth": "opencode",
                "session_id": session_id,
                "opencode_session_id": opencode_session_id,
                "active": False,
                "active_run": None,
                "run": None,
                "reason": resolved.reason,
                "action_hint": "safe_to_send",
                "active_child_sessions": list(resolved.active_child_sessions),
                "diagnostics": _chat_run_state_diagnostics(
                    store,
                    resolved,
                    active_record=active_record,
                    latest_record=latest_record,
                    stale_runs=stale_runs,
                ),
            }
        )

    active_public = None
    if active_record is not None:
        active_public = await validate_chat_run_against_opencode(
            store=store,
            client=request.app[OPENCODE_CLIENT_KEY],
            record=active_record,
            event_bus=request.app.get(EVENT_BUS_KEY),
            resolved=resolved,
        )

    if active_public is not None and active_public.get("opencode_active") is True:
        active_run = active_run_public_from_store(
            active_public,
            portal_session_id=session_id,
            opencode_session_id=opencode_session_id,
            resolved=resolved,
        )
    else:
        active_run = synthetic_active_run_from_resolved(portal_session_id=session_id, opencode_session_id=opencode_session_id, resolved=resolved)
    return web.json_response(
        {
            "success": True,
            "engine": "opencode",
            "source_of_truth": "opencode",
            "session_id": session_id,
            "opencode_session_id": opencode_session_id,
            "active": True,
            "active_run": active_run,
            "run": active_run,
            "reason": "opencode_status_active",
            "action_hint": "wait_reconnect_or_stop",
            "active_child_sessions": list(resolved.active_child_sessions),
            "diagnostics": _chat_run_state_diagnostics(
                store,
                resolved,
                active_record=active_record,
                latest_record=latest_record,
                active_public=active_public,
            ),
        }
    )


async def session_status_handler(request: web.Request) -> web.Response:
    chat_run_store = request.app.get(CHAT_RUN_STORE_KEY)
    session_id = request.match_info["session_id"]
    state = await _resolve_portal_session_opencode_state(request, session_id)
    opencode_session_id = state["opencode_session_id"]
    resolved = state["resolved"]
    status_type = resolved.status or "unknown"
    active_run = None
    active_record = None
    active_public = None
    if chat_run_store is not None and hasattr(chat_run_store, "active_for_session"):
        active_record = chat_run_store.active_for_session(session_id)
    latest_record = chat_run_store.latest_for_session(session_id) if chat_run_store is not None and hasattr(chat_run_store, "latest_for_session") else None
    stale_runs = []
    if resolved.active and active_record is not None:
        active_public = await validate_chat_run_against_opencode(
            store=chat_run_store,
            client=request.app[OPENCODE_CLIENT_KEY],
            record=active_record,
            event_bus=request.app.get(EVENT_BUS_KEY),
            resolved=resolved,
        )
    elif not resolved.active and chat_run_store is not None:
        stale_runs = _mark_session_runs_stale_from_opencode(
            chat_run_store,
            session_id,
            "chat_run_store_stale_because_opencode_idle",
            resolved=resolved,
        )
    if resolved.active:
        if active_public is not None and active_public.get("opencode_active") is True:
            active_run = active_run_public_from_store(
                active_public,
                portal_session_id=session_id,
                opencode_session_id=opencode_session_id,
                resolved=resolved,
            )
        else:
            active_run = synthetic_active_run_from_resolved(portal_session_id=session_id, opencode_session_id=opencode_session_id, resolved=resolved)

    return web.json_response(
        {
            "success": True,
            "engine": "opencode",
            "source_of_truth": "opencode",
            "session_id": session_id,
            "opencode_session_id": opencode_session_id,
            "exists": bool(resolved.exists),
            "status": {"type": status_type},
            "status_type": status_type,
            "active": bool(resolved.active),
            "can_abort": bool(resolved.active and resolved.exists),
            "reason": resolved.reason,
            "action_hint": "wait_reconnect_or_stop" if resolved.active else "safe_to_send",
            "active_run": active_run,
            "active_child_sessions": list(resolved.active_child_sessions),
            "diagnostics": _chat_run_state_diagnostics(
                chat_run_store,
                resolved,
                active_record=active_record,
                latest_record=latest_record,
                active_public=active_public,
                stale_runs=stale_runs,
            ),
        }
    )


def _abort_result_succeeded(abort_result: dict[str, Any]) -> bool:
    if not isinstance(abort_result, dict):
        return False
    if abort_result.get("success") is True:
        return True
    missing = abort_result.get("missing_session_ids")
    errors = abort_result.get("errors")
    if missing and not errors:
        return True
    return False


def _abort_result_failed(abort_result: dict[str, Any]) -> bool:
    return not _abort_result_succeeded(abort_result)


def _abort_result_aborted(abort_result: dict[str, Any], opencode_session_id: str) -> bool:
    if _abort_result_failed(abort_result) or abort_result.get("skipped"):
        return False
    aborted = abort_result.get("aborted_session_ids")
    if isinstance(aborted, list) and aborted:
        return not opencode_session_id or opencode_session_id in {str(item) for item in aborted if item}
    missing = abort_result.get("missing_session_ids")
    if isinstance(missing, list) and opencode_session_id in {str(item) for item in missing if item}:
        return False
    return bool(opencode_session_id)


async def _abort_opencode_session(client, opencode_session_id: str) -> dict[str, Any]:
    if not opencode_session_id:
        return {"success": True, "supported": True, "skipped": True, "reason": "missing_opencode_session_id"}
    if hasattr(client, "abort_session_tree"):
        return await client.abort_session_tree(opencode_session_id)
    if hasattr(client, "abort_session"):
        return await client.abort_session(opencode_session_id)
    return {"success": False, "supported": False, "error": "opencode_abort_unsupported"}


def _abort_result_missing_current_session(abort_result: dict[str, Any], opencode_session_id: str) -> bool:
    if not isinstance(abort_result, dict):
        return False
    errors = abort_result.get("errors")
    if errors:
        return False
    missing = abort_result.get("missing_session_ids")
    if not isinstance(missing, list) or not missing:
        return False
    missing_ids = {str(item) for item in missing if item}
    if opencode_session_id and opencode_session_id in missing_ids:
        return True
    aborted = abort_result.get("aborted_session_ids")
    return not aborted


async def _publish_abort_event(request: web.Request, event_type: str, *, record, abort_result: dict[str, Any], state: str = "aborted", summary: str | None = None) -> None:
    bus = request.app.get(EVENT_BUS_KEY)
    if bus is None or not hasattr(bus, "publish"):
        return
    event = {
        "type": event_type,
        "event_type": event_type,
        "engine": "opencode",
        "session_id": getattr(record, "portal_session_id", ""),
        "request_id": getattr(record, "request_id", ""),
        "opencode_session_id": getattr(record, "opencode_session_id", ""),
        "state": state,
        "summary": summary or event_type,
        "data": safe_preview({"abort_result": abort_result}, 2000),
        "created_at": utc_now_iso(),
        "ts": time.time(),
    }
    await bus.publish(event)


async def _handle_abort_failure(request: web.Request, *, store, client, record, abort_result: dict[str, Any]) -> web.Response:
    current = store.record_abort_failure(record.request_id, abort_result) or record
    await _publish_abort_event(request, "chat.run.abort_failed", record=current, abort_result=abort_result, state="failed", summary="OpenCode abort failed.")
    await _publish_abort_event(request, "opencode.session.abort_failed", record=current, abort_result=abort_result, state="failed", summary="OpenCode abort failed.")
    public = store.to_public_dict(current)
    try:
        validated = await validate_chat_run_against_opencode(store=store, client=client, record=current, event_bus=request.app.get(EVENT_BUS_KEY))
        if validated is not None:
            public = validated
        else:
            refreshed = store.get(record.request_id)
            public = store.to_public_dict(refreshed) if refreshed is not None else public
    except Exception:
        pass
    return web.json_response(
        {
            "success": False,
            "engine": "opencode",
            "source_of_truth": "opencode",
            "error": "opencode_abort_failed",
            "active": True,
            "action_hint": "wait_reconnect_or_stop",
            "run": public,
            "abort_result": safe_preview(abort_result, 2000),
        },
        status=409,
    )


async def _handle_abort_missing(request: web.Request, *, store, record, abort_result: dict[str, Any]) -> web.Response:
    stale = store.mark_stale(record.request_id, reason="opencode_session_missing_after_abort", metadata={"abort_result": abort_result}) or record
    await _publish_abort_event(request, "chat.run.stale", record=stale, abort_result=abort_result, state="stale", summary="OpenCode session missing after abort.")
    await _publish_abort_event(request, "opencode.session.missing", record=stale, abort_result=abort_result, state="stale", summary="OpenCode session missing after abort.")
    return web.json_response(
        {
            "success": True,
            "engine": "opencode",
            "source_of_truth": "opencode",
            "stale": True,
            "active": False,
            "action_hint": "safe_to_send",
            "status": {"type": "missing"},
            "reason": "opencode_session_missing_after_abort",
            "run": store.to_public_dict(stale),
            "abort_result": safe_preview(abort_result, 2000),
        }
    )


def _still_active_abort_payload(
    *,
    portal_session_id: str,
    opencode_session_id: str,
    resolved: ResolvedOpenCodeRunState,
    abort_result: dict[str, Any],
    can_hard_reset: bool,
) -> dict[str, Any]:
    return {
        "success": False,
        "engine": "opencode",
        "source_of_truth": "opencode",
        "error": "opencode_abort_still_active",
        "active": True,
        "active_run": synthetic_active_run_from_resolved(
            portal_session_id=portal_session_id,
            opencode_session_id=opencode_session_id,
            resolved=resolved,
        ),
        "action_hint": "hard_reset_or_new_session",
        "can_hard_reset": bool(can_hard_reset),
        "status": {"type": resolved.status or "unknown"},
        "status_type": resolved.status or "unknown",
        "abort_result": safe_preview(abort_result, 2000),
        "diagnostics": run_state_diagnostics(resolved),
    }


async def abort_chat_run_handler(request: web.Request) -> web.Response:
    store = request.app[CHAT_RUN_STORE_KEY]
    client = request.app[OPENCODE_CLIENT_KEY]
    request_id = request.match_info["request_id"]
    record = store.get(request_id)
    if record is None:
        return web.json_response({"success": False, "engine": "opencode", "error": "chat_run_not_found"}, status=404)
    abort_result = await _abort_opencode_session(client, record.opencode_session_id)
    if _abort_result_failed(abort_result):
        return await _handle_abort_failure(request, store=store, client=client, record=record, abort_result=abort_result)
    resolved = await _wait_until_opencode_inactive(client, record.opencode_session_id)
    if not resolved.active:
        if not resolved.exists or _abort_result_missing_current_session(abort_result, record.opencode_session_id):
            return await _handle_abort_missing(request, store=store, record=record, abort_result=abort_result)
        updated = store.mark_aborted(
            request_id,
            reason="user_aborted",
            metadata={
                "abort_result": safe_preview(abort_result, 2000),
                "source_of_truth": "opencode",
                "opencode_active": False,
                "opencode_status": resolved.status,
                "diagnostics": run_state_diagnostics(resolved),
            },
        ) or record
        await _publish_abort_event(request, "chat.run.aborted", record=updated, abort_result=abort_result)
        await _publish_abort_event(request, "opencode.session.aborted", record=updated, abort_result=abort_result)
        return web.json_response(
            {
                "success": True,
                "engine": "opencode",
                "source_of_truth": "opencode",
                "active": False,
                "action_hint": "safe_to_send",
                "status": {"type": resolved.status or "idle"},
                "status_type": resolved.status or "idle",
                "run": store.to_public_dict(updated),
                "abort_result": safe_preview(abort_result, 2000),
                "diagnostics": run_state_diagnostics(resolved),
            }
        )

    current = store.record_abort_failure(record.request_id, abort_result, reason="opencode_abort_still_active") or record
    await _publish_abort_event(
        request,
        "opencode.session.abort_still_active",
        record=current,
        abort_result=abort_result,
        state="active",
        summary="OpenCode abort returned but the session is still active.",
    )
    return web.json_response(
        _still_active_abort_payload(
            portal_session_id=record.portal_session_id,
            opencode_session_id=record.opencode_session_id,
            resolved=resolved,
            abort_result=abort_result,
            can_hard_reset=True,
        ),
        status=409,
    )


async def abort_session_handler(request: web.Request) -> web.Response:
    store = request.app[CHAT_RUN_STORE_KEY]
    client = request.app[OPENCODE_CLIENT_KEY]
    portal_session_id = request.match_info["session_id"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    force_detach = body.get("force_detach")
    if force_detach is None:
        force_detach = body.get("force_detach_on_still_active")
    force_detach = force_detach is not False

    state = await _resolve_portal_session_opencode_state(request, portal_session_id)
    session_record = state["session_record"]
    opencode_session_id = state["opencode_session_id"]
    if not opencode_session_id:
        stale_runs = _mark_session_runs_stale_from_opencode(
            store,
            portal_session_id,
            "missing_opencode_session_id",
            resolved=state["resolved"],
        )
        return web.json_response(
            {
                "success": True,
                "engine": "opencode",
                "source_of_truth": "opencode",
                "session_id": portal_session_id,
                "opencode_session_id": "",
                "active": False,
                "aborted": False,
                "status": {"type": "missing"},
                "status_type": "missing",
                "reason": "missing_opencode_session_id",
                "action_hint": "safe_to_send",
                "run": None,
                "diagnostics": {
                    "missing_opencode_session_id": True,
                    "chat_run_store_stale_count": len(stale_runs),
                },
            }
        )

    abort_result = await _abort_opencode_session(client, opencode_session_id)
    try:
        resolved = await _wait_until_opencode_inactive(client, opencode_session_id)
    except Exception as exc:
        if _abort_result_failed(abort_result):
            return web.json_response(
                {
                    "success": False,
                    "engine": "opencode",
                    "source_of_truth": "opencode",
                    "error": "opencode_abort_failed",
                    "session_id": portal_session_id,
                    "opencode_session_id": opencode_session_id,
                    "active": True,
                    "action_hint": "hard_reset_or_new_session" if force_detach else "wait_reconnect_or_stop",
                    "can_hard_reset": bool(force_detach),
                    "abort_result": safe_preview(abort_result, 2000),
                    "diagnostics": {"post_abort_status_error": safe_preview(str(exc), 1000)},
                },
                status=409,
            )
        raise

    aborted = _abort_result_aborted(abort_result, opencode_session_id)
    if not resolved.active:
        updated_runs = _mark_session_runs_aborted_or_stale(
            store,
            portal_session_id,
            "user_aborted" if aborted else "opencode_session_missing_after_abort" if not resolved.exists else "opencode_not_active_after_abort",
            abort_result=abort_result,
            resolved=resolved,
            aborted=aborted,
        )
        await _publish_session_event(
            request,
            "opencode.session.aborted" if aborted else "opencode.session.inactive_after_abort",
            portal_session_id=portal_session_id,
            opencode_session_id=opencode_session_id,
            state="aborted" if aborted else "inactive",
            summary="OpenCode session is inactive after abort.",
            data={"abort_result": safe_preview(abort_result, 2000), "updated_run_count": len(updated_runs)},
        )
        return web.json_response(
            {
                "success": True,
                "engine": "opencode",
                "source_of_truth": "opencode",
                "session_id": portal_session_id,
                "opencode_session_id": opencode_session_id,
                "active": False,
                "aborted": aborted,
                "status": {"type": resolved.status or "idle"},
                "status_type": resolved.status or "idle",
                "reason": resolved.reason,
                "action_hint": "safe_to_send",
                "run": store.to_public_dict(updated_runs[0]) if updated_runs else None,
                "abort_result": safe_preview(abort_result, 2000),
                "diagnostics": {
                    **run_state_diagnostics(resolved),
                    "chat_run_store_updated_count": len(updated_runs),
                    "abort_result_succeeded": _abort_result_succeeded(abort_result),
                },
            }
        )

    if not force_detach:
        if _abort_result_failed(abort_result):
            error = "opencode_abort_failed"
            payload = {
                "success": False,
                "engine": "opencode",
                "source_of_truth": "opencode",
                "error": error,
                "session_id": portal_session_id,
                "opencode_session_id": opencode_session_id,
                "active": True,
                "action_hint": "hard_reset_or_new_session",
                "can_hard_reset": True,
                "abort_result": safe_preview(abort_result, 2000),
                "diagnostics": run_state_diagnostics(resolved),
            }
        else:
            payload = _still_active_abort_payload(
                portal_session_id=portal_session_id,
                opencode_session_id=opencode_session_id,
                resolved=resolved,
                abort_result=abort_result,
                can_hard_reset=True,
            )
            payload["session_id"] = portal_session_id
            payload["opencode_session_id"] = opencode_session_id
        return web.json_response(payload, status=409)

    try:
        reset = await _hard_reset_portal_session_binding(
            request,
            portal_session_id=portal_session_id,
            old_opencode_session_id=opencode_session_id,
            abort_result=abort_result,
            old_resolved=resolved,
            reason="opencode_abort_still_active_detached",
        )
    except Exception as exc:
        payload = _still_active_abort_payload(
            portal_session_id=portal_session_id,
            opencode_session_id=opencode_session_id,
            resolved=resolved,
            abort_result=abort_result,
            can_hard_reset=True,
        )
        payload["session_id"] = portal_session_id
        payload["opencode_session_id"] = opencode_session_id
        payload["diagnostics"] = {
            **payload.get("diagnostics", {}),
            "hard_reset_error": safe_preview(str(exc), 1000),
            "abort_result_succeeded": _abort_result_succeeded(abort_result),
        }
        if _abort_result_failed(abort_result):
            payload["error"] = "opencode_abort_failed"
        return web.json_response(payload, status=409)

    aborted = _abort_result_aborted(abort_result, opencode_session_id)
    return web.json_response(
        {
            "success": True,
            "engine": "opencode",
            "source_of_truth": "opencode",
            "session_id": portal_session_id,
            "opencode_session_id": reset["new_opencode_session_id"],
            "old_opencode_session_id": opencode_session_id,
            "detached_old_session": True,
            "active": False,
            "aborted": False,
            "status": {"type": "idle"},
            "status_type": "idle",
            "action_hint": "safe_to_send",
            "message": "OpenCode abort did not make the previous session idle, so the portal session was rebound to a fresh OpenCode session.",
            "run": None,
            "abort_result": safe_preview(abort_result, 2000),
            "diagnostics": {
                "old_resolved": run_state_diagnostics(resolved),
                "new_opencode_session_id": reset["new_opencode_session_id"],
                "chat_run_store_stale_count": len(reset["stale_runs"]),
                "abort_result_succeeded": _abort_result_succeeded(abort_result),
            },
        }
    )


async def hard_reset_session_handler(request: web.Request) -> web.Response:
    client = request.app[OPENCODE_CLIENT_KEY]
    portal_session_id = request.match_info["session_id"]
    state = await _resolve_portal_session_opencode_state(request, portal_session_id)
    session_record = state["session_record"]
    old_opencode_session_id = state["opencode_session_id"]
    if session_record is None or getattr(session_record, "deleted", False):
        return web.json_response(
            {
                "success": False,
                "engine": "opencode",
                "source_of_truth": "opencode",
                "error": "missing_session_binding",
                "session_id": portal_session_id,
                "active": False,
                "action_hint": "new_session_required",
            },
            status=404,
        )

    abort_result: dict[str, Any] = {"success": True, "skipped": True, "reason": "missing_opencode_session_id"}
    if old_opencode_session_id:
        try:
            abort_result = await _abort_opencode_session(client, old_opencode_session_id)
        except Exception as exc:
            abort_result = {
                "success": False,
                "error": "opencode_abort_failed",
                "detail": safe_preview(str(exc), 1000),
            }

    reset = await _hard_reset_portal_session_binding(
        request,
        portal_session_id=portal_session_id,
        old_opencode_session_id=old_opencode_session_id,
        abort_result=abort_result,
        old_resolved=state["resolved"],
        reason="explicit_hard_reset",
    )
    return web.json_response(
        {
            "success": True,
            "engine": "opencode",
            "source_of_truth": "opencode",
            "session_id": portal_session_id,
            "detached_old_session": True,
            "old_opencode_session_id": old_opencode_session_id,
            "opencode_session_id": reset["new_opencode_session_id"],
            "active": False,
            "aborted": False,
            "status": {"type": "idle"},
            "status_type": "idle",
            "action_hint": "safe_to_send",
            "abort_result": safe_preview(abort_result, 2000),
            "diagnostics": {
                "old_resolved": run_state_diagnostics(state["resolved"]),
                "new_opencode_session_id": reset["new_opencode_session_id"],
                "chat_run_store_stale_count": len(reset["stale_runs"]),
            },
        }
    )


async def reconcile_chat_runs_on_startup(app: web.Application) -> dict[str, int]:
    store = app.get(CHAT_RUN_STORE_KEY)
    client = app.get(OPENCODE_CLIENT_KEY)
    if store is None or client is None or not hasattr(store, "list_active"):
        return {"chat_runs_validated": 0, "chat_runs_stale_marked": 0, "chat_runs_still_active": 0}
    records = list(store.list_active(include_detached_candidates=True))
    summary = {"chat_runs_validated": 0, "chat_runs_stale_marked": 0, "chat_runs_still_active": 0}
    for record in records:
        before_status = getattr(record, "status", "")
        try:
            public = await validate_chat_run_against_opencode(store=store, client=client, record=record, event_bus=app.get(EVENT_BUS_KEY))
        except Exception:
            continue
        summary["chat_runs_validated"] += 1
        refreshed = store.get(getattr(record, "request_id", "")) if hasattr(store, "get") else None
        if public is not None and public.get("opencode_active") is True:
            summary["chat_runs_still_active"] += 1
        if before_status != "stale" and getattr(refreshed, "status", "") == "stale":
            summary["chat_runs_stale_marked"] += 1
    bus = app.get(EVENT_BUS_KEY)
    if bus is not None and hasattr(bus, "publish"):
        await bus.publish(
            {
                "type": "chat.runs.reconciled",
                "event_type": "chat.runs.reconciled",
                "engine": "opencode",
                "state": "completed",
                "summary": "Chat run startup reconciliation completed.",
                "data": dict(summary),
                "created_at": utc_now_iso(),
                "ts": time.time(),
            }
        )
    return summary


async def internal_opencode_status_handler(request: web.Request) -> web.Response:
    client = request.app[OPENCODE_CLIENT_KEY]
    manager = request.app.get(OPENCODE_PROCESS_MANAGER_KEY)
    try:
        health = await client.health()
    except Exception as exc:
        health = {"healthy": False, "error": safe_preview(str(exc), 500)}
    process = manager.status_snapshot() if manager is not None and hasattr(manager, "status_snapshot") else {"managed": False}
    return web.json_response(
        {
            "success": True,
            "engine": "opencode",
            "process": safe_preview(process, 4000),
            "health": safe_preview(health, 1000),
            "last_restart": {
                "reason": process.get("last_restart_reason") if isinstance(process, dict) else None,
                "at": process.get("last_restart_at") if isinstance(process, dict) else None,
            },
        }
    )


async def internal_opencode_log_tail_handler(request: web.Request) -> web.Response:
    settings: Settings = request.app[SETTINGS_KEY]
    manager = request.app.get(OPENCODE_PROCESS_MANAGER_KEY)
    try:
        lines = int(request.query.get("lines", "200"))
    except ValueError:
        lines = 200
    lines = max(1, min(lines, 2000))
    if manager is not None and hasattr(manager, "log_tail"):
        text = manager.log_tail(lines)
    else:
        log_path = settings.adapter_state_dir / "opencode-serve.log"
        try:
            text = "\n".join(log_path.read_text(encoding="utf-8", errors="ignore").splitlines()[-lines:])
        except Exception:
            text = ""
    return web.json_response({"success": True, "engine": "opencode", "lines": lines, "log_tail": safe_preview(text, 20000)})


async def internal_chat_run_diagnostics_handler(request: web.Request) -> web.Response:
    store = request.app[CHAT_RUN_STORE_KEY]
    request_id = request.match_info["request_id"]
    record = store.get(request_id)
    if record is None:
        return web.json_response({"success": False, "engine": "opencode", "error": "chat_run_not_found"}, status=404)
    public = store.to_public_dict(record, include_final_payload=False)
    diagnostics = public.get("diagnostics") if isinstance(public, dict) else {}
    return web.json_response(
        {
            "success": True,
            "engine": "opencode",
            "request_id": request_id,
            "status": getattr(record, "status", ""),
            "diagnostics": safe_preview(diagnostics or {}, 4000),
        }
    )


def create_app(settings: Settings, opencode_client: OpenCodeClient | None = None, *, start_event_bridge: bool | None = None, opencode_process_manager: OpenCodeProcessManager | None = None) -> web.Application:
    app = web.Application()
    app[SETTINGS_KEY] = settings
    state_paths = ensure_state_dirs(settings)
    app[STATE_PATHS_KEY] = state_paths
    app[SESSION_STORE_KEY] = SessionStore(state_paths.sessions_dir)
    app[TASK_STORE_KEY] = TaskStore(state_paths.tasks_dir)
    app[CHATLOG_STORE_KEY] = ChatLogStore(state_paths.chatlogs_dir)
    app[CHAT_RUN_STORE_KEY] = ChatRunStore(state_paths.chat_runs_file)
    app[OPENCODE_BINDING_STORE_KEY] = OpenCodeBindingStore(state_paths.root / "opencode_conversation_bindings.json")
    app[USER_DISPLAY_STORE_KEY] = UserDisplayStore(state_paths.sessions_dir / "user_display_messages.json")
    app[USAGE_TRACKER_KEY] = UsageTracker(state_paths.usage_file)
    app[EVENT_BUS_KEY] = EventBus(settings.event_replay_limit, settings.event_replay_ttl_seconds)
    app[REQUEST_BINDING_STORE_KEY] = RequestBindingStore()
    app[TASK_BACKGROUND_TASKS_KEY] = set()
    injected_client = opencode_client is not None
    client = opencode_client or OpenCodeClient(settings)
    app[OPENCODE_CLIENT_KEY] = client
    if opencode_process_manager is not None:
        if getattr(opencode_process_manager, "event_bus", None) is None:
            opencode_process_manager.event_bus = app[EVENT_BUS_KEY]
        app[OPENCODE_PROCESS_MANAGER_KEY] = opencode_process_manager
    app.on_cleanup.append(cleanup_task_background_tasks)
    app[PORTAL_METADATA_CLIENT_KEY] = PortalMetadataClient(settings, pending_file=state_paths.portal_metadata_pending_file)
    app[RECOVERY_MANAGER_KEY] = RecoveryManager(settings=settings, state_paths=state_paths, session_store=app[SESSION_STORE_KEY], chatlog_store=app[CHATLOG_STORE_KEY], opencode_client=app[OPENCODE_CLIENT_KEY])
    managed_opencode = opencode_process_manager is not None
    should_start_event_bridge = settings.event_bridge_enabled and (start_event_bridge if start_event_bridge is not None else (not injected_client or managed_opencode)) and hasattr(client, "event_stream")
    if should_start_event_bridge:
        app[EVENT_BRIDGE_KEY] = OpenCodeEventBridge(settings, client, app[EVENT_BUS_KEY], app[SESSION_STORE_KEY], app[TASK_STORE_KEY], app[CHATLOG_STORE_KEY], app[REQUEST_BINDING_STORE_KEY])
    register_file_routes(app)
    app.router.add_get("/health", health_handler)
    app.router.add_get("/actuator/health", health_handler)
    app.router.add_post("/api/internal/runtime-profile/apply", runtime_profile_apply_handler)
    app.router.add_get("/api/internal/runtime-profile/status", runtime_profile_status_handler)
    app.router.add_get("/api/internal/opencode/status", internal_opencode_status_handler)
    app.router.add_get("/api/internal/opencode/log-tail", internal_opencode_log_tail_handler)
    app.router.add_get("/api/internal/chat/runs/{request_id}/diagnostics", internal_chat_run_diagnostics_handler)
    app.router.add_get("/api/internal/opencode-effective-config", effective_config_handler)
    app.router.add_get("/api/capabilities", capabilities_handler)
    app.router.add_get("/api/queue/status", queue_status_handler)
    app.router.add_get("/api/skills", skills_handler)
    app.router.add_get("/api/git-info", git_info_handler)
    app.router.add_get("/api/skill-git-info", skill_git_info_handler)
    app.router.add_get("/api/agent/system-prompt/config", system_prompt_config_get_handler)
    app.router.add_put("/api/agent/system-prompt/config", system_prompt_config_put_handler)
    app.router.add_get("/api/agent/system-prompt/{name}", system_prompt_get_handler)
    app.router.add_put("/api/agent/system-prompt/{name}", system_prompt_put_handler)
    app.router.add_post("/api/chat", chat_handler)
    app.router.add_post("/api/chat/stream", chat_stream_handler)
    app.router.add_get("/api/chat/runs", list_chat_runs_handler)
    app.router.add_get("/api/chat/runs/{request_id}", get_chat_run_handler)
    app.router.add_post("/api/chat/runs/{request_id}/abort", abort_chat_run_handler)
    app.router.add_post("/api/tasks/execute", execute_task_handler)
    app.router.add_get("/api/tasks/{task_id}", get_task_handler)
    app.router.add_post("/api/tasks/{task_id}/cancel", cancel_task_handler)
    app.router.add_get("/api/events", events_ws_handler)
    app.router.add_get("/api/usage", usage_handler)
    app.router.add_post("/api/permissions/{permission_id}/respond", permission_respond_handler)
    register_opencode_thin_routes(app)
    app.router.add_get("/api/sessions", list_sessions_handler)
    app.router.add_post("/api/clear", clear_sessions_handler)
    app.router.add_get("/api/sessions/{session_id}/active-run", active_chat_run_for_session_handler)
    app.router.add_get("/api/sessions/{session_id}/status", session_status_handler)
    app.router.add_post("/api/sessions/{session_id}/abort", abort_session_handler)
    app.router.add_post("/api/sessions/{session_id}/hard-reset", hard_reset_session_handler)
    app.router.add_get("/api/sessions/{session_id}/chatlog", session_chatlog_handler)
    app.router.add_post("/api/sessions/{session_id}/rename", rename_session_handler)
    app.router.add_post("/api/sessions/{session_id}/messages/{message_id}/edit/async", edit_message_async_handler)
    app.router.add_post("/api/sessions/{session_id}/messages/{message_id}/edit", edit_message_handler)
    app.router.add_post("/api/sessions/{session_id}/messages/{message_id}/delete-from-here", delete_message_from_here_handler)
    app.router.add_get("/api/sessions/{session_id}", get_session_handler)
    app.router.add_delete("/api/sessions/{session_id}", delete_session_handler)

    async def _run_recovery(app):
        try:
            summary = await app[RECOVERY_MANAGER_KEY].recover()
            print(f"recovery summary: {summary}")
        except Exception as exc:
            print(f"recovery failed: {exc}")

    app.on_startup.append(_run_recovery)
    async def _managed_opencode_startup(app):
        manager = app.get(OPENCODE_PROCESS_MANAGER_KEY)
        if manager:
            env_path = settings.adapter_state_dir / "opencode.env"
            if path_exists(env_path):
                env = _merge_startup_env_with_process_fallback(settings, read_runtime_env_file(env_path))
            else:
                env = build_runtime_env_from_config(settings, {}).env
            write_git_gh_auth_assets(settings, env)
            await manager.start(env, reason="startup")
            if hasattr(manager, "run_watchdog"):
                app[OPENCODE_WATCHDOG_TASK_KEY] = asyncio.create_task(manager.run_watchdog(app=app))

    async def _reconcile_chat_runs(app):
        try:
            summary = await reconcile_chat_runs_on_startup(app)
            print(f"chat run reconciliation summary: {summary}")
        except Exception as exc:
            print(f"chat run reconciliation failed: {exc}")

    async def _start_event_bridge(app):
        bridge = app.get(EVENT_BRIDGE_KEY)
        if bridge:
            app[EVENT_BRIDGE_TASK_KEY] = asyncio.create_task(bridge.run_forever())
    async def _cleanup_event_bridge(app):
        task = app.get(EVENT_BRIDGE_TASK_KEY)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    app.on_startup.append(_managed_opencode_startup)
    app.on_startup.append(_reconcile_chat_runs)
    if should_start_event_bridge:
        app.on_startup.append(_start_event_bridge)
        app.on_cleanup.append(_cleanup_event_bridge)
    async def _cleanup_opencode_watchdog(app):
        task = app.get(OPENCODE_WATCHDOG_TASK_KEY)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    async def _stop_managed_opencode(app):
        manager = app.get(OPENCODE_PROCESS_MANAGER_KEY)
        if manager:
            await manager.stop()
    app.on_cleanup.append(_cleanup_opencode_watchdog)
    app.on_cleanup.append(_stop_managed_opencode)
    return app


async def run_server(host: str, port: int, settings: Settings) -> None:
    print(f"adapter listening on {host}:{port}")
    print(f"opencode url {settings.opencode_url}")
    print(f"configured opencode version {settings.opencode_version or 'not enforced'}")
    app = create_app(settings)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    while True:
        await __import__("asyncio").sleep(3600)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--opencode-url", default=None)
    parser.add_argument("--manage-opencode", action="store_true")
    args = parser.parse_args()
    settings = Settings.from_env(opencode_url=args.opencode_url)
    print(f"adapter listening on {args.host}:{args.port}")
    print(f"opencode url {settings.opencode_url}")
    print(f"configured opencode version {settings.opencode_version or 'not enforced'}")
    if args.manage_opencode:
        client = OpenCodeClient(settings)
        manager = OpenCodeProcessManager(settings, client)
        app = create_app(settings, client, opencode_process_manager=manager)
    else:
        app = create_app(settings)
    web.run_app(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
