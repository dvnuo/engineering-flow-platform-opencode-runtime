from __future__ import annotations

import argparse
from datetime import datetime, timezone

from aiohttp import web

from .capabilities import build_capability_catalog
from .chat_api import chat_handler, chat_stream_handler
from .event_bus import EventBus, events_ws_handler
from .file_routes import register_file_routes
from .opencode_client import OpenCodeClient
from .opencode_config import build_opencode_config, write_main_agent_prompt, write_opencode_config
from .profile_store import ProfileOverlay, ProfileOverlayStore, sanitize_public_secrets
from .session_store import SessionStore
from .task_store import TaskStore
from .tasks_api import cleanup_task_background_tasks, execute_task_handler, get_task_handler
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
from .state import ensure_state_dirs


async def health_handler(request: web.Request) -> web.Response:
    settings: Settings = request.app["settings"]
    client: OpenCodeClient = request.app["opencode_client"]
    try:
        info = await client.health()
    except Exception:
        info = {"healthy": False, "error": "unavailable"}
    healthy = bool(info.get("healthy"))
    payload = {
        "status": "ok" if healthy else "degraded",
        "service": "efp-opencode-runtime",
        "engine": "opencode",
        "opencode_version": settings.opencode_version,
        "opencode": {"healthy": healthy},
    }
    if healthy:
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
    settings: Settings = request.app["settings"]
    client = request.app["opencode_client"]
    runtime_config = payload["config"]
    runtime_profile_id = payload.get("runtime_profile_id")
    revision = payload.get("revision")
    write_main_agent_prompt(settings)
    generated_config, config_hash, updated_sections = build_opencode_config(settings, runtime_config)
    write_opencode_config(settings, generated_config)
    warnings: list[str] = []
    llm = runtime_config.get("llm") if isinstance(runtime_config.get("llm"), dict) else {}
    if llm and any(key in llm for key in ("provider", "model", "api_key", "temperature", "max_tokens")) and "llm" not in updated_sections:
        updated_sections.append("llm")
    provider, api_key = llm.get("provider"), llm.get("api_key")
    if provider and api_key:
        if hasattr(client, "put_auth"):
            try:
                auth_result = await client.put_auth(provider, api_key)
            except Exception:
                auth_result = {"success": False, "error": "auth update failed"}
            if auth_result.get("success"):
                pass
            elif auth_result.get("skipped"):
                warnings.append("opencode auth update skipped")
            else:
                warnings.append("opencode auth update failed; manual auth or restart may be required")
        else:
            warnings.append("opencode auth update skipped")
    pending_restart = True
    if hasattr(client, "patch_config"):
        try:
            result = await client.patch_config(generated_config)
        except Exception:
            result = {"success": False, "pending_restart": True}
        pending_restart = bool(result.get("pending_restart", not result.get("success", False)))
    if pending_restart:
        warnings.append("opencode config patch unsupported; restart may be required")
    ProfileOverlayStore(settings).save(
        ProfileOverlay(runtime_profile_id=runtime_profile_id, revision=revision, config=runtime_config, applied_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), generated_config_hash=config_hash)
    )
    return web.json_response({"success": True, "engine": "opencode", "runtime_profile_id": runtime_profile_id, "revision": revision, "updated_sections": updated_sections, "config_hash": config_hash, "pending_restart": pending_restart, "warnings": warnings})


async def capabilities_handler(request: web.Request) -> web.Response:
    try:
        payload = await build_capability_catalog(request.app["settings"], request.app["opencode_client"])
        return web.json_response(payload)
    except Exception:
        return web.json_response({"error": "capabilities unavailable", "engine": "opencode"}, status=500)


def create_app(settings: Settings, opencode_client: OpenCodeClient | None = None) -> web.Application:
    app = web.Application()
    app["settings"] = settings
    state_paths = ensure_state_dirs(settings)
    app["state_paths"] = state_paths
    app["session_store"] = SessionStore(state_paths.sessions_dir)
    app["task_store"] = TaskStore(state_paths.tasks_dir)
    app["event_bus"] = EventBus()
    app["task_background_tasks"] = set()
    app["opencode_client"] = opencode_client or OpenCodeClient(settings)
    app.on_cleanup.append(cleanup_task_background_tasks)
    register_file_routes(app)
    app.router.add_get("/health", health_handler)
    app.router.add_get("/actuator/health", health_handler)
    app.router.add_post("/api/internal/runtime-profile/apply", runtime_profile_apply_handler)
    app.router.add_get("/api/capabilities", capabilities_handler)
    app.router.add_post("/api/chat", chat_handler)
    app.router.add_post("/api/chat/stream", chat_stream_handler)
    app.router.add_post("/api/tasks/execute", execute_task_handler)
    app.router.add_get("/api/tasks/{task_id}", get_task_handler)
    app.router.add_get("/api/events", events_ws_handler)
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
