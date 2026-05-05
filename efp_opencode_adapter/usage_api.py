from aiohttp import web
from .app_keys import *


async def usage_handler(request: web.Request) -> web.Response:
    tracker = request.app[USAGE_TRACKER_KEY]
    try:
        days = int(request.query.get("days", "30"))
    except Exception:
        days = 30
    if days < 1 or days > 365:
        days = 30
    return web.json_response(tracker.summarize(days=days))
