from __future__ import annotations

import logging

from aiohttp import web

from .app_keys import SETTINGS_KEY
from .personalization import load_personalization

logger = logging.getLogger(__name__)


def requested_card_version(raw: str | None) -> int:
    """The card contract version a caller asked for with `?v=`.

    Only a plain integer of 2 or more opts in. A missing, malformed or lower
    value means a caller that predates the parameter, and it must keep
    receiving exactly the cards it always did.
    """
    text = (raw or "").strip()
    if not (text.isascii() and text.isdigit()):
        return 1
    try:
        version = int(text)
    except ValueError:
        # Python refuses to convert a digit string of several thousand
        # characters; nobody asking for a real version sends one.
        return 1
    return version if version >= 2 else 1


async def personalization_handler(request: web.Request) -> web.Response:
    """GET /api/personalization[?v=2]

    Serves the greeting and starter cards from the agents-repo branch this
    assistant booted with, so Portal never has to clone that repo itself.
    """
    settings = request.app[SETTINGS_KEY]
    try:
        payload = load_personalization(
            settings.workspace_dir,
            version=requested_card_version(request.query.get("v")),
        )
    except Exception:
        # A member with no greeting is a smaller failure than a member with no
        # chat, so this degrades rather than raising.
        logger.warning("Failed to load assistant personalization", exc_info=True)
        payload = {"welcome": None, "cards": []}
    return web.json_response(payload)
