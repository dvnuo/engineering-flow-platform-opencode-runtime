from __future__ import annotations

import argparse

from aiohttp import web

from .chat_api import chat_handler, chat_stream_handler
from .event_bus import EventBus, events_ws_handler
from .file_routes import register_file_routes
from .opencode_client import OpenCodeClient
from .session_store import SessionStore
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
    info = await client.health()
    healthy = bool(info.get("healthy"))
    payload = {
        "status": "ok" if healthy else "degraded",
        "service": "efp-opencode-runtime",
        "engine": "opencode",
        "opencode_version": settings.opencode_version,
        "opencode": {
            "healthy": healthy,
        },
    }
    if healthy:
        payload["opencode"]["version"] = info.get("version")
    else:
        payload["opencode"]["error"] = info.get("error", "unavailable")
    return web.json_response(payload, status=200 if healthy else 503)


def create_app(settings: Settings, opencode_client: OpenCodeClient | None = None) -> web.Application:
    app = web.Application()
    app["settings"] = settings
    state_paths = ensure_state_dirs(settings)
    app["state_paths"] = state_paths
    app["session_store"] = SessionStore(state_paths.sessions_dir)
    app["event_bus"] = EventBus()
    app["opencode_client"] = opencode_client or OpenCodeClient(settings)
    register_file_routes(app)
    app.router.add_get("/health", health_handler)
    app.router.add_get("/actuator/health", health_handler)
    app.router.add_post("/api/chat", chat_handler)
    app.router.add_post("/api/chat/stream", chat_stream_handler)
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
