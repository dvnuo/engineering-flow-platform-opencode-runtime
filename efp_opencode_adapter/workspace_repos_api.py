from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from .app_keys import SETTINGS_KEY
from .workspace_repos import list_workspace_repos

logger = logging.getLogger(__name__)


async def workspace_repos_handler(request: web.Request) -> web.Response:
    """GET /api/workspace/repos

    Lists the git repositories cloned in this assistant's workspace, so Portal
    can offer their branches in a picker. It takes no parameters on purpose:
    which directories are looked at is the runtime's decision, never the
    caller's.
    """
    settings = request.app[SETTINGS_KEY]
    try:
        # Dozens of short git subprocesses: off the event loop, or every
        # stream this assistant is serving would stall behind them.
        payload = await asyncio.to_thread(
            list_workspace_repos,
            settings.workspace_dir,
            settings.workspace_repos_dir,
        )
    except Exception:
        # A picker with no suggestions is a smaller failure than an error the
        # member has to dismiss, so this degrades rather than raising.
        logger.warning("Failed to list workspace repositories", exc_info=True)
        payload = {"repos": [], "truncated": False}
    return web.json_response(payload)
