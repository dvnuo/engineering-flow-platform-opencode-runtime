from __future__ import annotations

import logging

from aiohttp import web

from .app_keys import SETTINGS_KEY
from .personalization import load_personalization

logger = logging.getLogger(__name__)


async def personalization_handler(request: web.Request) -> web.Response:
    """GET /api/personalization

    Serves the greeting and starter cards from the agents-repo branch this
    assistant booted with, so Portal never has to clone that repo itself.
    """
    settings = request.app[SETTINGS_KEY]
    try:
        payload = load_personalization(settings.workspace_dir)
    except Exception:
        # A member with no greeting is a smaller failure than a member with no
        # chat, so this degrades rather than raising.
        logger.warning("Failed to load assistant personalization", exc_info=True)
        payload = {"welcome": None, "cards": []}
    return web.json_response(payload)
