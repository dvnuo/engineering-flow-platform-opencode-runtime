from __future__ import annotations

import asyncio
import time
from typing import Any

import aiohttp

from .settings import Settings


class OpenCodeClient:
    def __init__(self, settings: Settings, session: aiohttp.ClientSession | None = None):
        self.settings = settings
        self._session = session

    async def health(self) -> dict[str, Any]:
        auth = None
        if self.settings.opencode_server_password:
            auth = aiohttp.BasicAuth(self.settings.opencode_server_username, self.settings.opencode_server_password)
        url = f"{self.settings.opencode_url.rstrip('/')}/global/health"

        if self._session is not None:
            return await self._do_health(self._session, url, auth)

        async with aiohttp.ClientSession() as session:
            return await self._do_health(session, url, auth)

    async def _do_health(self, session: aiohttp.ClientSession, url: str, auth: aiohttp.BasicAuth | None) -> dict[str, Any]:
        try:
            async with session.get(url, auth=auth, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                status = resp.status
                if status != 200:
                    return {"healthy": False, "error": f"unexpected status {status}", "status": status}
                try:
                    payload = await resp.json()
                except Exception as exc:
                    return {"healthy": False, "error": f"invalid json: {exc}", "status": status}
                return {
                    "healthy": bool(payload.get("healthy", False)),
                    "version": payload.get("version"),
                    "raw": payload,
                }
        except Exception as exc:
            return {"healthy": False, "error": str(exc)}

    async def wait_until_ready(self, timeout_seconds: int = 60) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            result = await self.health()
            version = result.get("version")
            if version and version != self.settings.opencode_version:
                raise RuntimeError(
                    f"opencode version mismatch: expected {self.settings.opencode_version}, got {version}"
                )
            if result.get("healthy"):
                return
            await asyncio.sleep(0.5)
        raise TimeoutError(f"opencode did not become ready within {timeout_seconds} seconds")
