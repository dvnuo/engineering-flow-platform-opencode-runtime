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

    def _auth(self) -> aiohttp.BasicAuth | None:
        if self.settings.opencode_server_password:
            return aiohttp.BasicAuth(self.settings.opencode_server_username, self.settings.opencode_server_password)
        return None

    async def health(self) -> dict[str, Any]:
        url = f"{self.settings.opencode_url.rstrip('/')}/global/health"
        if self._session is not None:
            return await self._do_health(self._session, url, self._auth())
        async with aiohttp.ClientSession() as session:
            return await self._do_health(session, url, self._auth())

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
                return {"healthy": bool(payload.get("healthy", False)), "version": payload.get("version"), "raw": payload}
        except Exception as exc:
            return {"healthy": False, "error": str(exc)}

    async def put_auth(self, provider: str, api_key: str) -> dict[str, Any]:
        if not provider or not api_key:
            return {"success": False, "skipped": True}
        url = f"{self.settings.opencode_url.rstrip('/')}/auth/{provider}"
        payload = {"provider": provider, "api_key": api_key}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.put(url, auth=self._auth(), json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if 200 <= resp.status < 300:
                        return {"success": True, "status": resp.status}
                    return {"success": False, "status": resp.status, "error": "auth update failed"}
        except Exception as exc:
            return {"success": False, "error": str(exc).replace(api_key, "***REDACTED***")}

    async def patch_config(self, config: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.settings.opencode_url.rstrip('/')}/config"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.patch(url, auth=self._auth(), json=config, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if 200 <= resp.status < 300:
                        return {"success": True, "status": resp.status}
                    return {"success": False, "pending_restart": True, "status": resp.status, "error": "config patch unsupported"}
        except Exception:
            return {"success": False, "pending_restart": True, "error": "config patch unsupported"}

    async def mcp(self) -> dict[str, Any]:
        url = f"{self.settings.opencode_url.rstrip('/')}/mcp"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, auth=self._auth(), timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status // 100 != 2:
                        return {"success": False, "tools": []}
                    payload = await resp.json()
                    if not isinstance(payload, dict):
                        return {"success": False, "tools": []}
                    return {"success": True, "tools": payload.get("tools", [])}
        except Exception:
            return {"success": False, "tools": []}

    async def wait_until_ready(self, timeout_seconds: int = 60) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            result = await self.health()
            version = result.get("version")
            if version and version != self.settings.opencode_version:
                raise RuntimeError(f"opencode version mismatch: expected {self.settings.opencode_version}, got {version}")
            if result.get("healthy"):
                return
            await asyncio.sleep(0.5)
        raise TimeoutError(f"opencode did not become ready within {timeout_seconds} seconds")
