from __future__ import annotations

import argparse
from datetime import datetime, timezone

from aiohttp import web
from .app_keys import (
    SETTINGS_KEY,
    STATE_PATHS_KEY,
    SESSION_STORE_KEY,
    TASK_STORE_KEY,
    CHATLOG_STORE_KEY,
    USAGE_TRACKER_KEY,
    EVENT_BUS_KEY,
    TASK_BACKGROUND_TASKS_KEY,
    OPENCODE_CLIENT_KEY,
    PORTAL_METADATA_CLIENT_KEY,
    RECOVERY_MANAGER_KEY,
    EVENT_BRIDGE_KEY,
    EVENT_BRIDGE_TASK_KEY,
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
from .opencode_client import OpenCodeClient
from .permissions_api import permission_respond_handler
from .portal_metadata_client import PortalMetadataClient
from .recovery import RecoveryManager
from .usage_api import usage_handler
from .usage_tracker import UsageTracker
from .opencode_config import build_opencode_config, write_main_agent_prompt, write_opencode_config
from .profile_store import ProfileOverlay, ProfileOverlayStore, build_profile_status_payload, sanitize_profile_config_for_storage, sanitize_public_secrets
from .session_store import SessionStore
from .task_store import TaskStore
from .tasks_api import cancel_task_handler, cleanup_task_background_tasks, execute_task_handler, get_task_handler
from .sessions_api import (
    clear_sessions_handler,
    delete_session_handler,
    get_session_handler,
    list_sessions_handler,
    rename_session_handler,
    session_chatlog_handler,
    unsupported_message_mutation_handler,
)
from .settings import Settings
from .state import build_state_health_snapshot, ensure_state_dirs
import asyncio


async def health_handler(request: web.Request) -> web.Response:
    settings: Settings = request.app[SETTINGS_KEY]
    client: OpenCodeClient = request.app[OPENCODE_CLIENT_KEY]
    try:
        info = await client.health()
    except Exception:
        info = {"healthy": False, "error": "unavailable"}
    opencode_healthy = bool(info.get("healthy"))
    state_health = build_state_health_snapshot(settings, request.app[STATE_PATHS_KEY])
    state_healthy = bool(state_health.get("healthy"))
    bridge = request.app.get(EVENT_BRIDGE_KEY)
    event_bridge_status = bridge.status_snapshot() if bridge and hasattr(bridge, "status_snapshot") else {"enabled": False, "running": False}
    healthy = opencode_healthy and state_healthy
    payload = {
        "status": "ok" if healthy else "degraded",
        "service": "efp-opencode-runtime",
        "engine": "opencode",
        "opencode_version": settings.opencode_version,
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
    write_main_agent_prompt(settings)
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
    if llm and any(key in llm for key in ("provider", "model", "api_key", "temperature", "max_tokens")) and "llm" not in updated_sections:
        updated_sections.append("llm")
    provider, api_key = llm.get("provider"), llm.get("api_key")
    auth_update_status = "skipped"
    if provider and api_key:
        if hasattr(client, "put_auth"):
            try:
                auth_result = await client.put_auth(provider, api_key)
            except Exception:
                auth_result = {"success": False, "error": "auth update failed"}
            if auth_result.get("success"):
                auth_update_status = "updated"
            elif auth_result.get("skipped"):
                warnings.append("opencode auth update skipped")
                auth_update_status = "skipped"
            else:
                warnings.append("opencode auth update failed; manual auth or restart may be required")
                auth_update_status = "failed"
        else:
            warnings.append("opencode auth update skipped")
    patch_result: dict = {"success": False, "pending_restart": True}
    if hasattr(client, "patch_config"):
        try:
            patch_result = await client.patch_config(generated_config)
        except Exception:
            patch_result = {"success": False, "pending_restart": True}
        pending_restart = bool(patch_result.get("pending_restart", not patch_result.get("success", False)))
    if pending_restart:
        warnings.append("opencode config patch unsupported; restart may be required")
    if pending_restart:
        status, applied = "pending_restart", False
    elif auth_update_status == "failed":
        status, applied = "partially_applied", False
    else:
        status, applied = "applied", True
    ProfileOverlayStore(settings).save(ProfileOverlay(runtime_profile_id=runtime_profile_id, revision=revision, config=sanitize_profile_config_for_storage(runtime_config), applied_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), generated_config_hash=config_hash, status=status, pending_restart=pending_restart, warnings=warnings, updated_sections=updated_sections, last_apply_error=last_error, applied=applied))
    return web.json_response({"success": True, "engine": "opencode", "runtime_profile_id": runtime_profile_id, "revision": revision, "status": status, "applied": applied, "config_written": config_written, "updated_sections": updated_sections, "config_hash": config_hash, "pending_restart": pending_restart, "warnings": warnings, "patch_config_result": sanitize_public_secrets({"success": patch_result.get("success"), "pending_restart": patch_result.get("pending_restart"), "status": patch_result.get("status")}), "auth_update_status": auth_update_status, "status_endpoint": "/api/internal/runtime-profile/status"})


async def runtime_profile_status_handler(request: web.Request) -> web.Response:
    return web.json_response(build_profile_status_payload(request.app[SETTINGS_KEY]))


async def capabilities_handler(request: web.Request) -> web.Response:
    try:
        payload = await build_capability_catalog(request.app[SETTINGS_KEY], request.app[OPENCODE_CLIENT_KEY])
        return web.json_response(payload)
    except Exception:
        return web.json_response({"error": "capabilities unavailable", "engine": "opencode"}, status=500)


def create_app(settings: Settings, opencode_client: OpenCodeClient | None = None, *, start_event_bridge: bool | None = None) -> web.Application:
    app = web.Application()
    app[SETTINGS_KEY] = settings
    state_paths = ensure_state_dirs(settings)
    app[STATE_PATHS_KEY] = state_paths
    app[SESSION_STORE_KEY] = SessionStore(state_paths.sessions_dir)
    app[TASK_STORE_KEY] = TaskStore(state_paths.tasks_dir)
    app[CHATLOG_STORE_KEY] = ChatLogStore(state_paths.chatlogs_dir)
    app[USAGE_TRACKER_KEY] = UsageTracker(state_paths.usage_file)
    app[EVENT_BUS_KEY] = EventBus()
    app[TASK_BACKGROUND_TASKS_KEY] = set()
    injected_client = opencode_client is not None
    client = opencode_client or OpenCodeClient(settings)
    app[OPENCODE_CLIENT_KEY] = client
    app.on_cleanup.append(cleanup_task_background_tasks)
    app[PORTAL_METADATA_CLIENT_KEY] = PortalMetadataClient(settings, pending_file=state_paths.portal_metadata_pending_file)
    app[RECOVERY_MANAGER_KEY] = RecoveryManager(settings=settings, state_paths=state_paths, session_store=app[SESSION_STORE_KEY], chatlog_store=app[CHATLOG_STORE_KEY], opencode_client=app[OPENCODE_CLIENT_KEY])
    should_start_event_bridge = settings.event_bridge_enabled and (start_event_bridge if start_event_bridge is not None else not injected_client) and hasattr(client, "event_stream")
    if should_start_event_bridge:
        app[EVENT_BRIDGE_KEY] = OpenCodeEventBridge(settings, client, app[EVENT_BUS_KEY], app[SESSION_STORE_KEY], app[TASK_STORE_KEY], app[CHATLOG_STORE_KEY])
    register_file_routes(app)
    app.router.add_get("/health", health_handler)
    app.router.add_get("/actuator/health", health_handler)
    app.router.add_post("/api/internal/runtime-profile/apply", runtime_profile_apply_handler)
    app.router.add_get("/api/internal/runtime-profile/status", runtime_profile_status_handler)
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
    app.router.add_get("/api/sessions/{session_id}/chatlog", session_chatlog_handler)
    app.router.add_post("/api/sessions/{session_id}/rename", rename_session_handler)
    app.router.add_post("/api/sessions/{session_id}/messages/{message_id}/edit", unsupported_message_mutation_handler)
    app.router.add_post(
        "/api/sessions/{session_id}/messages/{message_id}/delete-from-here", unsupported_message_mutation_handler
    )
    app.router.add_get("/api/sessions/{session_id}", get_session_handler)
    app.router.add_delete("/api/sessions/{session_id}", delete_session_handler)

    async def _run_recovery(app):
        try:
            summary = await app[RECOVERY_MANAGER_KEY].recover()
            print(f"recovery summary: {summary}")
        except Exception as exc:
            print(f"recovery failed: {exc}")

    app.on_startup.append(_run_recovery)
    async def _start_event_bridge(app):
        bridge = app.get(EVENT_BRIDGE_KEY)
        if bridge:
            app[EVENT_BRIDGE_TASK_KEY] = asyncio.create_task(bridge.run_forever())
    async def _cleanup_event_bridge(app):
        task = app.get(EVENT_BRIDGE_TASK_KEY)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    if should_start_event_bridge:
        app.on_startup.append(_start_event_bridge)
        app.on_cleanup.append(_cleanup_event_bridge)
    return app


async def run_server(host: str, port: int, settings: Settings) -> None:
    print(f"adapter listening on {host}:{port}")
    print(f"opencode url {settings.opencode_url}")
    print(f"opencode version {settings.opencode_version}")
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
    args = parser.parse_args()
    settings = Settings.from_env(opencode_url=args.opencode_url)
    print(f"adapter listening on {args.host}:{args.port}")
    print(f"opencode url {settings.opencode_url}")
    print(f"opencode version {settings.opencode_version}")
    web.run_app(create_app(settings), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
