from __future__ import annotations

import argparse
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
    REQUEST_BINDING_STORE_KEY,
    COPILOT_TOKEN_MANAGER_KEY,
)

from .capabilities import build_capability_catalog
from .chat_api import chat_handler, chat_stream_handler
from .chatlog_store import ChatLogStore
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
from .opencode_client import OpenCodeClient, OpenCodeClientError
from .permissions_api import permission_respond_handler
from .portal_metadata_client import PortalMetadataClient
from .recovery import RecoveryManager
from .usage_api import usage_handler
from .usage_tracker import UsageTracker
from .agents_md import ensure_default_agents_md
from .atlassian_cli_config import write_atlassian_cli_config
from .opencode_config import build_opencode_config, normalize_opencode_provider_id, write_opencode_config
from .opencode_auth import build_opencode_auth_from_runtime_config, clear_opencode_auth_provider
from .copilot_plugin_auth import CopilotTokenManager, load_copilot_plugin_credential, save_or_clear_copilot_plugin_credential
from .copilot_proxy import copilot_proxy_handler
from .path_utils import path_exists
from .profile_store import ProfileOverlay, ProfileOverlayStore, build_profile_status_payload, sanitize_profile_config_for_storage, sanitize_public_secrets
from .runtime_env import aws_status_from_env, build_runtime_env_from_config, read_runtime_env_file, write_runtime_env_file
from .thinking_events import safe_preview
from .git_cli_auth import write_git_gh_auth_assets
from .opencode_process import OpenCodeProcessManager
from .session_store import SessionStore
from .skill_sync import sync_runtime_skills
from .task_store import TaskStore
from .tasks_api import cancel_task_handler, cleanup_task_background_tasks, execute_task_handler, get_task_handler, resume_active_task_collectors
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
AWS_ENV_KEYS = {
    "AWS_SHARED_CREDENTIALS_FILE",
}
STARTUP_FALLBACK_ENV_KEYS = GIT_GH_ENV_KEYS | AWS_ENV_KEYS | {"ATLASSIAN_CONFIG"}


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


def _is_atlassian_only_profile_change(runtime_config: dict, updated_sections: list[str]) -> bool:
    if not isinstance(runtime_config, dict) or not runtime_config:
        return False
    meaningful_keys = {str(key) for key, value in runtime_config.items() if value not in ({}, [], None, "")}
    if not meaningful_keys or not meaningful_keys.issubset({"jira", "confluence", "atlassian"}):
        return False
    return "atlassian" in set(updated_sections)


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
    try:
        copilot_credential_result = save_or_clear_copilot_plugin_credential(settings, runtime_config)
        clear_opencode_auth_provider(settings, "github-copilot")
    except Exception:
        last_error = "copilot_credential_state_failed"
        ProfileOverlayStore(settings).save(ProfileOverlay(runtime_profile_id=runtime_profile_id, revision=revision, config=sanitize_profile_config_for_storage(runtime_config), applied_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), generated_config_hash=config_hash, status="failed", pending_restart=False, warnings=warnings, updated_sections=updated_sections, last_apply_error=last_error, applied=False))
        return web.json_response({"success": False, "engine": "opencode", "status": "failed", "applied": False, "pending_restart": False, "config_written": config_written, "error": last_error, "warnings": warnings, "status_endpoint": "/api/internal/runtime-profile/status"}, status=500)
    llm = runtime_config.get("llm") if isinstance(runtime_config.get("llm"), dict) else {}
    if llm and any(key in llm for key in ("provider", "model", "api_key", "oauth", "temperature", "max_tokens")) and "llm" not in updated_sections:
        updated_sections.append("llm")
    if (copilot_credential_result.stored or copilot_credential_result.cleared) and "llm" not in updated_sections:
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
    aws_status = aws_status_from_env(env_result.env)
    aws_configured = bool("aws" in env_result.updated_sections and aws_status.get("configured"))
    combined_updated_sections = sorted(set(updated_sections + env_result.updated_sections + atlassian_result.updated_sections))
    manager = request.app.get(OPENCODE_PROCESS_MANAGER_KEY)
    restart_performed = False
    health_ok = None
    opencode_pid = None
    restart_meta = {}
    restart_deferred_reason = None
    if manager:
        if _is_atlassian_only_profile_change(runtime_config, combined_updated_sections):
            status, applied = "applied", True
            pending_restart = False
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
    overlay = ProfileOverlay(runtime_profile_id=runtime_profile_id, revision=revision, config=sanitize_profile_config_for_storage(runtime_config), applied_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), generated_config_hash=config_hash, status=status, pending_restart=pending_restart, warnings=warnings, updated_sections=combined_updated_sections, last_apply_error=last_error, applied=applied, env_hash=env_result.env_hash, env_path=str(env_path), restart_performed=restart_performed, opencode_pid=opencode_pid, last_restart_at=restart_meta.get("last_restart_at") if restart_meta else None, last_restart_reason=restart_meta.get("last_restart_reason") if restart_meta else None, health_ok=health_ok, git_auth_configured=bool(git_auth_result.get("configured")), gh_host=git_auth_result.get("host"), gh_config_dir=git_auth_result.get("gh_config_dir"), git_askpass_path=git_auth_result.get("askpass_path"), gitconfig_path=git_auth_result.get("gitconfig_path"), atlassian_cli_configured=atlassian_result.configured, atlassian_config_path=atlassian_result.path, atlassian_jira_instances=atlassian_result.jira_instances, atlassian_confluence_instances=atlassian_result.confluence_instances, aws_configured=aws_configured)
    ProfileOverlayStore(settings).save(overlay)
    response = {"success": True, "engine": "opencode", "runtime_profile_id": runtime_profile_id, "revision": revision, "status": status, "applied": applied, "config_written": config_written, "env_written": True, "env_hash": env_result.env_hash, "env_path": str(env_path), "updated_sections": combined_updated_sections, "config_hash": config_hash, "pending_restart": pending_restart, "warnings": warnings, "auth_update_status": auth_update_status, "auth_provider": auth_build.provider, "auth_type": auth_build.auth_type, "restart_performed": restart_performed, "opencode_pid": opencode_pid, "health_ok": health_ok, "git_auth_configured": bool(git_auth_result.get("configured")), "gh_host": git_auth_result.get("host"), "gh_config_dir": git_auth_result.get("gh_config_dir"), "git_askpass_path": git_auth_result.get("askpass_path"), "gitconfig_path": git_auth_result.get("gitconfig_path"), "atlassian_cli_configured": atlassian_result.configured, "atlassian_config_path": atlassian_result.path, "atlassian_jira_instances": atlassian_result.jira_instances, "atlassian_confluence_instances": atlassian_result.confluence_instances, "atlassian_status": atlassian_result.redacted_status, "aws_configured": aws_configured, "status_endpoint": "/api/internal/runtime-profile/status"}
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
        aws_status = aws_status_from_env(runtime_env)
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
            "aws_configured": bool(aws_status.get("configured")),
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
    auth_obj = auth.get(provider) if isinstance(auth, dict) and provider != "github-copilot" else None
    overlay = ProfileOverlayStore(settings).load()
    profile_cfg = overlay.config if overlay else {}
    github_cfg = profile_cfg.get("github") if isinstance(profile_cfg.get("github"), dict) else {}
    proxy_cfg = profile_cfg.get("proxy") if isinstance(profile_cfg.get("proxy"), dict) else {}
    runtime_env = _runtime_env_for_status(settings, overlay)
    aws_status = aws_status_from_env(runtime_env)
    env_token_present = bool(runtime_env.get("GH_TOKEN") or runtime_env.get("GITHUB_TOKEN") or runtime_env.get("GH_ENTERPRISE_TOKEN") or runtime_env.get("GITHUB_ENTERPRISE_TOKEN"))
    config_token_present = bool(github_cfg.get("api_token") or github_cfg.get("token") or github_cfg.get("access_token"))
    gh_host = github_cfg.get("host") or runtime_env.get("GH_HOST") or "github.com"
    git_askpass_path = runtime_env.get("GIT_ASKPASS")
    gitconfig_path = runtime_env.get("GIT_CONFIG_GLOBAL")
    git_askpass_present = bool(git_askpass_path and path_exists(Path(git_askpass_path)))
    gitconfig_present = bool(gitconfig_path and path_exists(Path(gitconfig_path)))
    git_auth_configured = bool(env_token_present and git_askpass_present and gitconfig_present)
    raw_provider_options = (((cfg.get("provider") or {}).get(provider) or {}).get("options") if provider else {}) or {}
    provider_options = sanitize_public_secrets(raw_provider_options)
    if not isinstance(provider_options, dict):
        provider_options = {}
    copilot_base_url_present = bool(raw_provider_options.get("baseURL")) if isinstance(raw_provider_options, dict) else False
    token_manager = request.app.get(COPILOT_TOKEN_MANAGER_KEY)
    if token_manager is not None and hasattr(token_manager, "status_snapshot"):
        copilot_snapshot = token_manager.status_snapshot()
    else:
        credential_present = load_copilot_plugin_credential(settings) is not None
        copilot_snapshot = {"credential_present": credential_present, "token_cached": False, "expires_at_present": False}
    copilot_credential_present = bool(copilot_snapshot.get("credential_present"))
    return web.json_response(
        {
            "engine": "opencode",
            "opencode_version": settings.opencode_version,
            "model": model,
            "provider": provider or None,
            "auth": {"provider": provider or None, "present": isinstance(auth_obj, dict), "type": auth_obj.get("type") if isinstance(auth_obj, dict) else None},
            "provider_options": provider_options,
            "config_path": str(settings.opencode_config_path),
            "profile": {
                "runtime_profile_id": overlay.runtime_profile_id if overlay else None,
                "revision": overlay.revision if overlay else None,
            },
            "runtime_integrations": {
                "github": {"enabled": bool(github_cfg) or env_token_present, "base_url": github_cfg.get("api_base_url") or runtime_env.get("GITHUB_API_BASE_URL") or "https://api.github.com", "host": gh_host, "token_present": config_token_present or env_token_present, "git_auth_configured": git_auth_configured, "gh_config_dir": runtime_env.get("GH_CONFIG_DIR"), "git_askpass_present": git_askpass_present, "gitconfig_present": gitconfig_present},
                "copilot": {"enabled": bool(provider == "github-copilot" or copilot_credential_present or copilot_base_url_present), "credential_present": copilot_credential_present, "token_cached": bool(copilot_snapshot.get("token_cached")), "base_url_present": copilot_base_url_present, "expires_at_present": bool(copilot_snapshot.get("expires_at_present"))},
                "proxy": {"enabled": bool(proxy_cfg.get("enabled")), "url_present": bool(proxy_cfg.get("url")), "password_present": bool(proxy_cfg.get("password"))},
                "aws": {"enabled": bool(aws_status.get("configured"))},
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


async def session_status_handler(request: web.Request) -> web.Response:
    session_id = request.match_info["session_id"]
    session_store = request.app[SESSION_STORE_KEY]
    client = request.app[OPENCODE_CLIENT_KEY]
    record = session_store.get(session_id)
    if record is None or getattr(record, "deleted", False):
        return web.json_response({"success": False, "engine": "opencode", "error": "session_not_found", "session_id": session_id}, status=404)

    opencode_session_id = str(getattr(record, "opencode_session_id", "") or "")
    session_payload: Any = {}
    upstream_status: Any = {}
    exists = bool(opencode_session_id)
    status_type = "unknown" if exists else "missing"
    status_error = ""

    if opencode_session_id:
        try:
            session_payload = await client.get_session(opencode_session_id)
            if isinstance(session_payload, dict):
                raw_status = session_payload.get("status") or session_payload.get("state") or session_payload.get("phase")
                if isinstance(raw_status, str) and raw_status.strip():
                    status_type = raw_status.strip().lower()
                else:
                    status_type = "available"
            else:
                status_type = "available"
        except OpenCodeClientError as exc:
            if exc.status == 404:
                exists = False
                status_type = "missing"
                status_error = str(exc)
            else:
                raise web.HTTPBadGateway(text=json.dumps({"error": "opencode_error", "detail": str(exc)}), content_type="application/json")
        except Exception as exc:
            status_error = safe_preview(str(exc), 500)

    if exists and hasattr(client, "get_session_status"):
        try:
            upstream_status = await client.get_session_status(timeout_seconds=30)
        except Exception as exc:
            upstream_status = {"error": safe_preview(str(exc), 500)}

    status_payload = {
        "type": status_type,
        "exists": exists,
        "session": safe_preview(session_payload, 2000),
        "upstream": safe_preview(upstream_status, 2000),
    }
    if status_error:
        status_payload["error"] = safe_preview(status_error, 500)

    return web.json_response(
        {
            "success": True,
            "engine": "opencode",
            "source_of_truth": "opencode",
            "session_id": session_id,
            "opencode_session_id": opencode_session_id,
            "exists": exists,
            "status": status_payload,
            "status_type": status_type,
            "metadata": {
                "engine": "opencode",
                "source_of_truth": "opencode",
                "partial_recovery": bool(getattr(record, "partial_recovery", False)),
            },
        }
    )


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


def create_app(settings: Settings, opencode_client: OpenCodeClient | None = None, *, start_event_bridge: bool | None = None, opencode_process_manager: OpenCodeProcessManager | None = None) -> web.Application:
    app = web.Application()
    app[SETTINGS_KEY] = settings
    state_paths = ensure_state_dirs(settings)
    app[STATE_PATHS_KEY] = state_paths
    app[SESSION_STORE_KEY] = SessionStore(state_paths.sessions_dir)
    app[TASK_STORE_KEY] = TaskStore(state_paths.tasks_dir)
    app[CHATLOG_STORE_KEY] = ChatLogStore(state_paths.chatlogs_dir)
    app[USER_DISPLAY_STORE_KEY] = UserDisplayStore(state_paths.sessions_dir / "user_display_messages.json")
    app[USAGE_TRACKER_KEY] = UsageTracker(state_paths.usage_file)
    app[EVENT_BUS_KEY] = EventBus(settings.event_replay_limit, settings.event_replay_ttl_seconds)
    app[REQUEST_BINDING_STORE_KEY] = RequestBindingStore()
    app[TASK_BACKGROUND_TASKS_KEY] = set()
    app[COPILOT_TOKEN_MANAGER_KEY] = CopilotTokenManager(settings)
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
    app.router.add_route("*", "/api/internal/copilot/{tail:.*}", copilot_proxy_handler)
    app.router.add_post("/api/internal/runtime-profile/apply", runtime_profile_apply_handler)
    app.router.add_get("/api/internal/runtime-profile/status", runtime_profile_status_handler)
    app.router.add_get("/api/internal/opencode/status", internal_opencode_status_handler)
    app.router.add_get("/api/internal/opencode/log-tail", internal_opencode_log_tail_handler)
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
    app.router.add_post("/api/tasks/execute", execute_task_handler)
    app.router.add_get("/api/tasks/{task_id}", get_task_handler)
    app.router.add_post("/api/tasks/{task_id}/cancel", cancel_task_handler)
    app.router.add_get("/api/events", events_ws_handler)
    app.router.add_get("/api/usage", usage_handler)
    app.router.add_post("/api/permissions/{permission_id}/respond", permission_respond_handler)
    app.router.add_get("/api/sessions", list_sessions_handler)
    app.router.add_post("/api/clear", clear_sessions_handler)
    app.router.add_get("/api/sessions/{session_id}/status", session_status_handler)
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
    async def _resume_task_collectors(app):
        try:
            resumed = resume_active_task_collectors(app)
            if resumed:
                print(f"resumed active task collectors: {resumed}")
        except Exception as exc:
            print(f"active task collector resume failed: {exc}")

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
    app.on_startup.append(_resume_task_collectors)
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
