from __future__ import annotations

import argparse
import os
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
    BOOT_PROJECTION_KEY,
)

from .capabilities import build_capability_catalog
from .chat_api import chat_handler, chat_run_cancel_handler, chat_run_status_handler, chat_stream_handler
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
from .opencode_config import normalize_opencode_provider_id
from .copilot_plugin_auth import CopilotTokenManager, load_copilot_plugin_credential
from .copilot_proxy import copilot_proxy_handler
from .path_utils import path_exists
from .portal_runtime_context_bootstrap import run_boot_projection_from_env
from .profile_store import ProfileOverlayStore, build_profile_status_payload, sanitize_public_secrets
from .runtime_env import aws_status_from_env, build_runtime_env_from_config, read_runtime_env_file
from .thinking_events import safe_preview
from .opencode_process import OpenCodeProcessManager
from .session_store import SessionStore
from .task_store import TaskStore
from .tasks_api import cancel_task_handler, cleanup_task_background_tasks, execute_task_handler, get_task_full_handler, get_task_handler
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
from .settings import (
    PROFILE_CONFIG_ENV,
    PROFILE_ID_ENV,
    Settings,
    profile_env_profile_id,
    profile_env_revision,
)
from .state import build_state_health_snapshot, ensure_state_dirs
import asyncio


def _boot_projection_snapshot(app: web.Application) -> dict[str, Any] | None:
    snapshot = app.get(BOOT_PROJECTION_KEY)
    return snapshot if isinstance(snapshot, dict) else None


def _boot_projection_complete(app: web.Application) -> bool:
    snapshot = _boot_projection_snapshot(app)
    return bool(snapshot and snapshot.get("ready"))


def _runtime_env_for_status(request: web.Request) -> dict[str, str]:
    """Runtime env for status reporting: the in-memory boot projection when
    available, else the opencode.env boot artifact, else the baseline env."""
    snapshot = _boot_projection_snapshot(request.app)
    if snapshot and isinstance(snapshot.get("env"), dict):
        return dict(snapshot["env"])
    settings: Settings = request.app[SETTINGS_KEY]
    env_path = settings.adapter_state_dir / "opencode.env"
    if path_exists(env_path):
        return read_runtime_env_file(env_path)
    return build_runtime_env_from_config(settings, {}).env


def _profile_identity_from_env(fallback_profile_id, fallback_revision) -> tuple[Any, Any]:
    profile_id = profile_env_profile_id() if os.getenv(PROFILE_ID_ENV) is not None else fallback_profile_id
    revision = profile_env_revision()
    return profile_id, revision if revision is not None else fallback_revision


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
    # Boot projection gate: in managed mode the adapter is not healthy until
    # the env-payload projection completed at startup.
    managed = request.app.get(OPENCODE_PROCESS_MANAGER_KEY) is not None
    projection_complete = _boot_projection_complete(request.app) if managed else True
    snapshot = _boot_projection_snapshot(request.app)
    profile_status = build_profile_status_payload(settings)
    profile_id, revision = _profile_identity_from_env(profile_status.get("runtime_profile_id"), profile_status.get("revision"))
    healthy = opencode_healthy and state_healthy and projection_complete
    payload = {
        "status": "ok" if healthy else "degraded",
        "service": "efp-opencode-runtime",
        "engine": "opencode",
        "opencode_version": observed_opencode_version or settings.opencode_version,
        "opencode": {"healthy": opencode_healthy},
        "state": state_health,
        "event_bridge": event_bridge_status,
        "profile": {
            "runtime_profile_id": profile_id,
            "revision": revision,
            "boot_projection_complete": projection_complete,
        },
    }
    if snapshot and snapshot.get("error"):
        payload["profile"]["boot_projection_error"] = snapshot.get("error")
    if opencode_healthy:
        payload["opencode"]["version"] = info.get("version")
    else:
        error = sanitize_public_secrets(str(info.get("error", "unavailable")))
        payload["opencode"]["error"] = error if isinstance(error, str) else "unavailable"
    return web.json_response(payload, status=200 if healthy else 503)


async def ready_handler(request: web.Request) -> web.Response:
    """Readiness gate: 200 only after the boot projection succeeded and the
    managed OpenCode child is healthy."""
    snapshot = _boot_projection_snapshot(request.app)
    if not snapshot or not snapshot.get("ready"):
        error = (snapshot or {}).get("error") or "boot projection not complete"
        return web.json_response({"ready": False, "error": error}, status=503)
    client = request.app[OPENCODE_CLIENT_KEY]
    try:
        info = await client.health()
    except Exception:
        info = {"healthy": False, "error": "unavailable"}
    if not info.get("healthy"):
        error = sanitize_public_secrets(str(info.get("error", "opencode unavailable")))
        return web.json_response({"ready": False, "error": error if isinstance(error, str) else "opencode unavailable"}, status=503)
    return web.json_response({
        "ready": True,
        "runtime_profile_id": snapshot.get("runtime_profile_id"),
        "revision": snapshot.get("revision"),
    })


async def runtime_profile_status_handler(request: web.Request) -> web.Response:
    settings: Settings = request.app[SETTINGS_KEY]
    payload = build_profile_status_payload(settings)
    # The pod env (EFP_PROFILE_ID/EFP_PROFILE_REVISION) is authoritative for
    # the running identity; the boot overlay is a projection detail record.
    profile_id, revision = _profile_identity_from_env(payload.get("runtime_profile_id"), payload.get("revision"))
    payload["runtime_profile_id"] = profile_id
    payload["revision"] = revision
    snapshot = _boot_projection_snapshot(request.app)
    payload["boot_projection"] = {
        "complete": bool(snapshot and snapshot.get("ready")),
        "error": snapshot.get("error") if snapshot else None,
        "applied_at": snapshot.get("applied_at") if snapshot else payload.get("applied_at"),
    }
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
    profile_id, profile_revision = _profile_identity_from_env(
        overlay.runtime_profile_id if overlay else None,
        overlay.revision if overlay else None,
    )
    profile_cfg = overlay.config if overlay else {}
    github_cfg = profile_cfg.get("github") if isinstance(profile_cfg.get("github"), dict) else {}
    mobile_cfg = profile_cfg.get("mobile-auto") if isinstance(profile_cfg.get("mobile-auto"), dict) else {}
    proxy_cfg = profile_cfg.get("proxy") if isinstance(profile_cfg.get("proxy"), dict) else {}
    runtime_env = _runtime_env_for_status(request)
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
                "runtime_profile_id": profile_id,
                "revision": profile_revision,
            },
            "runtime_integrations": {
                "github": {"enabled": bool(github_cfg) or env_token_present, "base_url": github_cfg.get("api_base_url") or runtime_env.get("GITHUB_API_BASE_URL") or "https://api.github.com", "host": gh_host, "token_present": config_token_present or env_token_present, "git_auth_configured": git_auth_configured, "gh_config_dir": runtime_env.get("GH_CONFIG_DIR"), "git_askpass_present": git_askpass_present, "gitconfig_present": gitconfig_present},
                "copilot": {"enabled": bool(provider == "github-copilot" or copilot_credential_present or copilot_base_url_present), "credential_present": copilot_credential_present, "token_cached": bool(copilot_snapshot.get("token_cached")), "base_url_present": copilot_base_url_present, "expires_at_present": bool(copilot_snapshot.get("expires_at_present"))},
                "proxy": {"enabled": bool(proxy_cfg.get("enabled")), "url_present": bool(proxy_cfg.get("url")), "password_present": bool(proxy_cfg.get("password"))},
                "aws": {"enabled": bool(aws_status.get("configured"))},
                "mobile-auto": {
                    "enabled": bool((mobile_cfg and mobile_cfg.get("enabled") is not False) or (overlay and overlay.mobile_cli_configured)),
                    "config_path": str(settings.efp_config_path),
                    "cli_configured": bool(overlay.mobile_cli_configured if overlay else path_exists(settings.efp_config_path)),
                    "state_dir": runtime_env.get("MOBILE_AUTO_STATE_DIR"),
                    "artifacts_dir": runtime_env.get("MOBILE_AUTO_ARTIFACTS_DIR"),
                    "browserstack_username_present": bool(runtime_env.get("BROWSERSTACK_USERNAME")),
                    "browserstack_access_key_present": bool(runtime_env.get("BROWSERSTACK_ACCESS_KEY")),
                    "local_binary": runtime_env.get("BROWSERSTACK_LOCAL_BINARY"),
                    "local_binary_present": bool(runtime_env.get("BROWSERSTACK_LOCAL_BINARY") and path_exists(Path(runtime_env["BROWSERSTACK_LOCAL_BINARY"]))),
                },
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


DEFAULT_MAX_UPLOAD_MB = 25
UPLOAD_TRANSPORT_HEADROOM_MB = 5


def resolve_upload_client_max_size() -> int:
    """aiohttp ``client_max_size`` (bytes) for request bodies incl. uploads.

    Sized from EFP_MAX_UPLOAD_MB (the user-facing per-file cap the Portal
    enforces) plus headroom for multipart / transport overhead so the adapter
    is never the gate for a file the Portal already accepted. Kept in parity
    with the native runtime.
    """
    raw = os.getenv("EFP_MAX_UPLOAD_MB", str(DEFAULT_MAX_UPLOAD_MB))
    try:
        mb = int(str(raw).strip())
    except (TypeError, ValueError):
        mb = DEFAULT_MAX_UPLOAD_MB
    if mb <= 0:
        mb = DEFAULT_MAX_UPLOAD_MB
    return (mb + UPLOAD_TRANSPORT_HEADROOM_MB) * 1024 * 1024


def create_app(settings: Settings, opencode_client: OpenCodeClient | None = None, *, start_event_bridge: bool | None = None, opencode_process_manager: OpenCodeProcessManager | None = None) -> web.Application:
    app = web.Application(client_max_size=resolve_upload_client_max_size())
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
    app.router.add_get("/ready", ready_handler)
    app.router.add_route("*", "/api/internal/copilot/{tail:.*}", copilot_proxy_handler)
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
    app.router.add_get("/api/chat/runs/{request_id}", chat_run_status_handler)
    app.router.add_post("/api/chat/runs/{request_id}/cancel", chat_run_cancel_handler)
    app.router.add_post("/api/tasks/execute", execute_task_handler)
    app.router.add_get("/api/tasks/{task_id}/full", get_task_full_handler)
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
    async def _managed_opencode_startup(app):
        manager = app.get(OPENCODE_PROCESS_MANAGER_KEY)
        if not manager:
            return
        # Boot ordering contract: projection -> scrub blob -> start opencode
        # with the projected env -> spawn watchdog. Blocking work is fine here:
        # nothing is served until startup completes, and readiness gates on it.
        try:
            projection = run_boot_projection_from_env(settings)
        except Exception as exc:
            error = sanitize_public_secrets(str(exc))
            if not isinstance(error, str):
                error = "boot projection failed"
            # Stay alive but unready: /ready (and managed /health) report 503
            # instead of crash-looping the container on a bad profile.
            app[BOOT_PROJECTION_KEY] = {"ready": False, "error": error}
            os.environ.pop(PROFILE_CONFIG_ENV, None)
            print(f"boot projection failed; adapter stays unready: {error}")
            return
        profile_id, revision = _profile_identity_from_env(projection.runtime_profile_id, projection.revision)
        app[BOOT_PROJECTION_KEY] = {
            "ready": True,
            "error": None,
            "runtime_profile_id": profile_id,
            "revision": revision,
            "applied_at": projection.applied_at,
            "warnings": list(projection.warnings),
            "env_hash": projection.env_hash,
            "env": dict(projection.env),
        }
        # Scrub the full Secret blob before any child process can spawn.
        os.environ.pop(PROFILE_CONFIG_ENV, None)
        await manager.start(projection.env, reason="startup")
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
