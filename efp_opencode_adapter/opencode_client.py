from __future__ import annotations

import asyncio
import time
from typing import Any

import aiohttp

from .settings import Settings


class OpenCodeClientError(Exception):
    def __init__(self, message: str, *, status: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.payload = payload


class OpenCodeClient:
    def __init__(self, settings: Settings, session: aiohttp.ClientSession | None = None):
        self.settings = settings
        self._session = session

    def _auth(self) -> aiohttp.BasicAuth | None:
        if self.settings.opencode_server_password:
            return aiohttp.BasicAuth(self.settings.opencode_server_username, self.settings.opencode_server_password)
        return None

    def _url(self, path: str) -> str:
        return f"{self.settings.opencode_url.rstrip('/')}{path}"

    async def _request_json(self, method: str, path: str, *, json: dict | None = None, expected_statuses: tuple[int, ...] = (200,), timeout_seconds: int = 30) -> Any:
        async def _run(session: aiohttp.ClientSession) -> Any:
            async with session.request(method, self._url(path), auth=self._auth(), json=json, timeout=aiohttp.ClientTimeout(total=timeout_seconds)) as resp:
                if resp.status not in expected_statuses:
                    try:
                        err_payload = await resp.json()
                    except Exception:
                        err_payload = await resp.text()
                    raise OpenCodeClientError(f"{method} {path} failed with status {resp.status}", status=resp.status, payload=err_payload)
                if resp.status == 204:
                    return None
                try:
                    return await resp.json()
                except Exception:
                    return await resp.text()

        if self._session is not None:
            return await _run(self._session)
        async with aiohttp.ClientSession() as session:
            return await _run(session)

    async def health(self) -> dict[str, Any]:
        url = self._url("/global/health")
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

    async def create_session(self, title: str | None = None) -> dict:
        return await self._request_json("POST", "/session", json={"title": title} if title else {}, expected_statuses=(200, 201))

    async def list_sessions(self) -> list[dict]:
        data = await self._request_json("GET", "/session")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("sessions") or data.get("data") or []
        return []

    async def get_session(self, session_id: str) -> dict:
        return await self._request_json("GET", f"/session/{session_id}")

    async def patch_session(self, session_id: str, title: str) -> dict:
        data = await self._request_json("PATCH", f"/session/{session_id}", json={"title": title}, expected_statuses=(200, 204))
        return data if data is not None else {"id": session_id, "title": title}

    async def delete_session(self, session_id: str) -> None:
        await self._request_json("DELETE", f"/session/{session_id}", expected_statuses=(200, 202, 204))

    async def list_messages(self, session_id: str) -> list[dict]:
        data = await self._request_json("GET", f"/session/{session_id}/message")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("messages") or data.get("data") or []
        return []

    async def send_message(self, session_id: str, *, parts: list[dict], model: str | None, agent: str | None, system: str | None = None) -> dict:
        payload: dict[str, Any] = {"parts": parts}
        if model:
            payload["model"] = model
        if agent:
            payload["agent"] = agent
        if system:
            payload["system"] = system
        return await self._request_json("POST", f"/session/{session_id}/message", json=payload, expected_statuses=(200, 201))

    async def prompt_async(self, session_id: str, payload: dict[str, Any]) -> dict:
        return await self._request_json("POST", f"/session/{session_id}/prompt_async", json=payload, expected_statuses=(200, 201, 202))

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
