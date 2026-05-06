from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import aiohttp

from .settings import Settings


class OpenCodeClientError(Exception):
    def __init__(self, message: str, *, status: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.payload = payload




async def _close_owned_response(resp: aiohttp.ClientResponse) -> None:
    session = getattr(resp, "_efp_session", None)
    if session is None:
        return
    try:
        resp.release()
    finally:
        await session.close()


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
            try:
                return await _run(self._session)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                raise OpenCodeClientError(f"{method} {path} transport error: {exc}", status=None, payload=None) from exc
        async with aiohttp.ClientSession() as session:
            try:
                return await _run(session)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                raise OpenCodeClientError(f"{method} {path} transport error: {exc}", status=None, payload=None) from exc

    async def _request_json_with_status(self, method: str, path: str, *, json: dict | None = None, expected_statuses: tuple[int, ...] = (200,), timeout_seconds: int = 30) -> tuple[int, Any]:
        async def _run(session: aiohttp.ClientSession) -> tuple[int, Any]:
            async with session.request(method, self._url(path), auth=self._auth(), json=json, timeout=aiohttp.ClientTimeout(total=timeout_seconds)) as resp:
                if resp.status not in expected_statuses:
                    try:
                        err_payload = await resp.json()
                    except Exception:
                        err_payload = await resp.text()
                    raise OpenCodeClientError(f"{method} {path} failed with status {resp.status}", status=resp.status, payload=err_payload)
                if resp.status == 204:
                    return resp.status, None
                try:
                    return resp.status, await resp.json()
                except Exception:
                    return resp.status, await resp.text()

        if self._session is not None:
            try:
                return await _run(self._session)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                raise OpenCodeClientError(f"{method} {path} transport error: {exc}", status=None, payload=None) from exc
        async with aiohttp.ClientSession() as session:
            try:
                return await _run(session)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                raise OpenCodeClientError(f"{method} {path} transport error: {exc}", status=None, payload=None) from exc

    async def _request(self, method: str, url: str, **kwargs):
        kwargs.setdefault("auth", self._auth())
        if self._session is not None:
            return await self._session.request(method, url, **kwargs)
        session = aiohttp.ClientSession()
        try:
            resp = await session.request(method, url, **kwargs)
        except Exception:
            await session.close()
            raise
        resp._efp_session = session
        return resp

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

    async def put_auth(self, provider: str, api_key: str) -> dict[str, Any]:
        if not provider or not api_key:
            return {"success": False, "skipped": True}
        url = f"{self.settings.opencode_url.rstrip('/')}/auth/{provider}"
        try:
            resp = await self._request("PUT", url, json={"provider": provider, "api_key": api_key}, timeout=aiohttp.ClientTimeout(total=10))
            try:
                if 200 <= resp.status < 300:
                    return {"success": True, "status": resp.status}
                return {"success": False, "status": resp.status, "error": "auth update failed"}
            finally:
                await _close_owned_response(resp)
        except Exception as exc:
            return {"success": False, "error": str(exc).replace(api_key, "***REDACTED***")}

    async def patch_config(self, config: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.settings.opencode_url.rstrip('/')}/config"
        try:
            resp = await self._request("PATCH", url, json=config, timeout=aiohttp.ClientTimeout(total=10))
            try:
                if 200 <= resp.status < 300:
                    return {"success": True, "status": resp.status}
                return {"success": False, "pending_restart": True, "status": resp.status, "error": "config patch unsupported"}
            finally:
                await _close_owned_response(resp)
        except Exception:
            return {"success": False, "pending_restart": True, "error": "config patch unsupported"}

    async def mcp(self) -> dict[str, Any]:
        url = f"{self.settings.opencode_url.rstrip('/')}/mcp"
        try:
            resp = await self._request("GET", url, timeout=aiohttp.ClientTimeout(total=5))
            try:
                if resp.status // 100 != 2:
                    return {"success": False, "tools": []}
                payload = await resp.json()
                return {"success": True, "tools": payload.get("tools", [])} if isinstance(payload, dict) else {"success": False, "tools": []}
            finally:
                await _close_owned_response(resp)
        except Exception:
            return {"success": False, "tools": []}

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

    async def get_message(self, session_id: str, message_id: str) -> dict:
        data = await self._request_json("GET", f"/session/{session_id}/message/{message_id}")
        return data if isinstance(data, dict) else {}

    async def send_message(
        self,
        session_id: str,
        *,
        parts: list[dict],
        model: str | None,
        agent: str | None,
        system: str | None = None,
        message_id: str | None = None,
        no_reply: bool | None = None,
        tools: dict | None = None,
    ) -> dict:
        payload: dict[str, Any] = {"parts": parts}
        if message_id:
            payload["messageID"] = message_id
        if no_reply is not None:
            payload["noReply"] = no_reply
        if tools:
            payload["tools"] = tools
        if model:
            payload["model"] = model
        if agent:
            payload["agent"] = agent
        if system:
            payload["system"] = system
        return await self._request_json("POST", f"/session/{session_id}/message", json=payload, expected_statuses=(200, 201))

    async def fork_session(self, session_id: str, message_id: str | None = None) -> dict:
        payload = {"messageID": message_id} if message_id else {}
        data = await self._request_json("POST", f"/session/{session_id}/fork", json=payload, expected_statuses=(200, 201))
        return data if isinstance(data, dict) else {}

    async def abort_session(self, session_id: str) -> dict[str, Any]:
        status, _ = await self._request_json_with_status("POST", f"/session/{session_id}/abort", expected_statuses=(200, 202, 204))
        return {"success": True, "supported": True, "status": status}

    async def revert_message(self, session_id: str, message_id: str, part_id: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"messageID": message_id}
        if part_id:
            payload["partID"] = part_id
        status, _ = await self._request_json_with_status("POST", f"/session/{session_id}/revert", json=payload, expected_statuses=(200, 202, 204))
        return {"success": True, "supported": True, "status": status}

    async def unrevert_session(self, session_id: str) -> dict[str, Any]:
        status, _ = await self._request_json_with_status("POST", f"/session/{session_id}/unrevert", expected_statuses=(200, 202, 204))
        return {"success": True, "supported": True, "status": status}

    async def prompt_async(self, session_id: str, payload: dict[str, Any]) -> dict | None:
        return await self._request_json("POST", f"/session/{session_id}/prompt_async", json=payload, expected_statuses=(200, 201, 202, 204))

    async def respond_permission(self, session_id: str, permission_id: str, payload: dict[str, Any]) -> dict:
        return await self._request_json(
            "POST",
            f"/session/{session_id}/permissions/{permission_id}",
            json=payload,
            expected_statuses=(200, 201, 202, 204),
        ) or {"success": True}

    async def cancel_message(self, session_id: str, message_id: str | None = None) -> dict[str, Any]:
        try:
            return await self.abort_session(session_id)
        except Exception:
            pass
        attempts = [
            ("POST", f"/session/{session_id}/cancel", {"messageID": message_id} if message_id else {}),
        ]
        if message_id:
            attempts[0:0] = [
                ("POST", f"/session/{session_id}/message/{message_id}/cancel", None),
                ("POST", f"/session/{session_id}/message/{message_id}/abort", None),
            ]
        for method, path, payload in attempts:
            try:
                resp = await self._request(method, self._url(path), json=payload, timeout=aiohttp.ClientTimeout(total=10))
                try:
                    if resp.status in {200, 202, 204}:
                        return {"success": True, "supported": True, "status": resp.status}
                    if resp.status in {404, 405}:
                        continue
                    return {"success": False, "supported": True, "status": resp.status}
                finally:
                    await _close_owned_response(resp)
            except Exception:
                continue
        return {"success": False, "supported": False, "reason": "cancel_endpoint_unsupported"}

    async def event_stream(self, *, global_events: bool = False, timeout_seconds: int | None = None) -> AsyncIterator[dict[str, Any]]:
        """Yield OpenCode SSE events.

        Callers that stop after partially consuming this async generator must call
        ``aclose()`` so an owned aiohttp ClientSession can be closed promptly.
        """
        path = "/global/event" if global_events else "/event"
        own_session = self._session is None
        session = self._session or aiohttp.ClientSession()
        timeout = aiohttp.ClientTimeout(total=timeout_seconds) if timeout_seconds is not None else aiohttp.ClientTimeout(total=None)
        try:
            async with session.get(self._url(path), auth=self._auth(), timeout=timeout) as resp:
                if resp.status != 200:
                    try:
                        payload = await resp.json()
                    except Exception:
                        payload = await resp.text()
                    raise OpenCodeClientError(f"GET {path} failed with status {resp.status}", status=resp.status, payload=payload)

                event_type: str | None = None
                data_lines: list[str] = []
                async for raw in resp.content:
                    line = raw.decode("utf-8").rstrip("\r\n")
                    if line.startswith("event:"):
                        event_type = line[len("event:"):].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[len("data:"):].strip())
                    elif line == "":
                        if event_type or data_lines:
                            raw_data = "\n".join(data_lines)
                            try:
                                event_payload = json.loads(raw_data) if raw_data else {}
                                if not isinstance(event_payload, dict):
                                    event_payload = {"data": event_payload}
                            except Exception:
                                event_payload = {"data": raw_data}
                            if event_type:
                                event_payload.setdefault("type", event_type)
                            elif "type" not in event_payload:
                                event_payload["type"] = "message"
                            yield event_payload
                        event_type = None
                        data_lines = []
        finally:
            if own_session:
                await session.close()

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
