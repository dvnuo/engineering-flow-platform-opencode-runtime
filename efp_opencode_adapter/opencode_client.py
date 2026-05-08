from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator
from typing import Any, Mapping

import aiohttp

from .settings import Settings
from .opencode_config import normalize_opencode_provider_id


class OpenCodeClientError(Exception):
    def __init__(self, message: str, *, status: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.payload = payload


def _format_transport_error(method: str, path: str, exc: BaseException) -> str:
    return f"{method} {path} transport error ({type(exc).__name__}): {_safe_error_preview(str(exc) or repr(exc))}"



_REDACT_KEYS = {"key", "api_key", "apikey", "access", "refresh", "access_token", "refresh_token", "token", "authorization", "password", "secret", "oauth"}
_SENSITIVE_TEXT_KEYS = {"key", "api_key", "apikey", "access", "refresh", "access_token", "refresh_token", "token", "authorization", "password", "secret", "oauth"}
COPILOT_INTEGRATION_HEADER = "copilot-integration-id"
COPILOT_INTEGRATION_ID = "vscode-chat"


def _redact_sensitive_text(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, (dict, list)):
                return json.dumps(_redact_sensitive(parsed), ensure_ascii=False)
        except Exception:
            pass
    key_union = "|".join(sorted(_SENSITIVE_TEXT_KEYS, key=len, reverse=True))
    out = text
    out = re.sub(
        rf"(?i)([\"']?(?:{key_union})[\"']?\s*:\s*[\"'])([^\"']+)([\"'])",
        r"\1***REDACTED***\3",
        out,
    )
    out = re.sub(
        rf"(?i)\b({key_union})\b(\s*[:=]\s*)([^\s,;&}}\]]+)",
        r"\1\2***REDACTED***",
        out,
    )
    patterns = [r"gho_[A-Za-z0-9_\-]+", r"ghu_[A-Za-z0-9_\-]+", r"ghp_[A-Za-z0-9_\-]+", r"github_pat_[A-Za-z0-9_\-]+", r"sk-[A-Za-z0-9_\-]+"]
    for pat in patterns:
        out = re.sub(pat, "***REDACTED***", out)
    return out


def _redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("***REDACTED***" if str(k).lower() in _REDACT_KEYS else _redact_sensitive(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_sensitive(v) for v in value]
    if isinstance(value, str):
        return _redact_sensitive_text(value)
    return value


def _safe_error_preview(value: Any, max_chars: int = 1000) -> str:
    redacted = _redact_sensitive(value)
    text = redacted if isinstance(redacted, str) else json.dumps(redacted, ensure_ascii=False)
    return text[:max_chars]


def _model_ref_from_value(model: Any) -> dict[str, str] | None:
    if isinstance(model, dict):
        provider_id = model.get("providerID")
        model_id = model.get("modelID")
        if isinstance(provider_id, str) and provider_id.strip() and isinstance(model_id, str) and model_id.strip():
            return {"providerID": normalize_opencode_provider_id(provider_id.strip()), "modelID": model_id.strip()}
        return None
    if isinstance(model, str):
        if "/" not in model:
            return None
        provider, model_id = model.split("/", 1)
        provider = normalize_opencode_provider_id(provider.strip())
        model_id = model_id.strip()
        if provider and model_id:
            return {"providerID": provider, "modelID": model_id}
    return None


def _copilot_integration_headers_for_model(model: str | None) -> dict[str, str] | None:
    if not isinstance(model, str) or "/" not in model:
        return None
    provider_prefix = model.split("/", 1)[0]
    provider = normalize_opencode_provider_id(provider_prefix)
    if provider == "github-copilot":
        return {COPILOT_INTEGRATION_HEADER: COPILOT_INTEGRATION_ID}
    return None


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

    def _url(self, path: str) -> str:
        return f"{self.settings.opencode_url.rstrip('/')}{path}"

    async def _request_json(self, method: str, path: str, *, json: dict | None = None, headers: dict[str, str] | None = None, expected_statuses: tuple[int, ...] = (200,), timeout_seconds: int = 30) -> Any:
        async def _run(session: aiohttp.ClientSession) -> Any:
            async with session.request(method, self._url(path), json=json, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout_seconds)) as resp:
                if resp.status not in expected_statuses:
                    try:
                        err_payload = await resp.json()
                    except Exception:
                        err_payload = await resp.text()
                    raise OpenCodeClientError(f"{method} {path} failed with status {resp.status}: {_safe_error_preview(err_payload)}", status=resp.status, payload=_redact_sensitive(err_payload))
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
                raise OpenCodeClientError(
                    _format_transport_error(method, path, exc),
                    status=None,
                    payload={"exception_type": type(exc).__name__, "exception": _safe_error_preview(str(exc) or repr(exc))},
                ) from exc
        async with aiohttp.ClientSession() as session:
            try:
                return await _run(session)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                raise OpenCodeClientError(
                    _format_transport_error(method, path, exc),
                    status=None,
                    payload={"exception_type": type(exc).__name__, "exception": _safe_error_preview(str(exc) or repr(exc))},
                ) from exc

    async def _request_json_with_status(self, method: str, path: str, *, json: dict | None = None, expected_statuses: tuple[int, ...] = (200,), timeout_seconds: int = 30) -> tuple[int, Any]:
        async def _run(session: aiohttp.ClientSession) -> tuple[int, Any]:
            async with session.request(method, self._url(path), json=json, timeout=aiohttp.ClientTimeout(total=timeout_seconds)) as resp:
                if resp.status not in expected_statuses:
                    try:
                        err_payload = await resp.json()
                    except Exception:
                        err_payload = await resp.text()
                    raise OpenCodeClientError(f"{method} {path} failed with status {resp.status}: {_safe_error_preview(err_payload)}", status=resp.status, payload=_redact_sensitive(err_payload))
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
                raise OpenCodeClientError(
                    _format_transport_error(method, path, exc),
                    status=None,
                    payload={"exception_type": type(exc).__name__, "exception": _safe_error_preview(str(exc) or repr(exc))},
                ) from exc
        async with aiohttp.ClientSession() as session:
            try:
                return await _run(session)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                raise OpenCodeClientError(
                    _format_transport_error(method, path, exc),
                    status=None,
                    payload={"exception_type": type(exc).__name__, "exception": _safe_error_preview(str(exc) or repr(exc))},
                ) from exc

    async def _request(self, method: str, url: str, **kwargs):
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
            return await self._do_health(self._session, url)
        async with aiohttp.ClientSession() as session:
            return await self._do_health(session, url)

    async def _do_health(self, session: aiohttp.ClientSession, url: str) -> dict[str, Any]:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
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

    async def put_auth_info(self, provider: str, auth_info: Mapping[str, Any]) -> dict[str, Any]:
        if not provider or not auth_info:
            return {"success": False, "skipped": True}
        url = f"{self.settings.opencode_url.rstrip('/')}/auth/{provider}"
        try:
            resp = await self._request("PUT", url, json=dict(auth_info), timeout=aiohttp.ClientTimeout(total=10))
            try:
                if 200 <= resp.status < 300:
                    return {"success": True, "status": resp.status, "auth_type": auth_info.get("type")}
                return {"success": False, "status": resp.status, "error": "auth update failed"}
            finally:
                await _close_owned_response(resp)
        except Exception as exc:
            return {"success": False, "error": _safe_error_preview(str(exc))}

    async def put_auth(self, provider: str, api_key: str) -> dict[str, Any]:
        return await self.put_auth_info(provider, {"type": "api", "key": api_key})

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

    async def list_tool_ids(self, timeout_seconds: int = 30) -> list[str]:
        data = await self._request_json(
            "GET",
            "/experimental/tool/ids",
            expected_statuses=(200,),
            timeout_seconds=timeout_seconds,
        )
        if isinstance(data, list) and all(isinstance(item, str) for item in data):
            return data
        if isinstance(data, dict):
            ids = data.get("ids")
            if isinstance(ids, list) and all(isinstance(item, str) for item in ids):
                return ids
            tools = data.get("tools")
            if isinstance(tools, list) and all(isinstance(item, str) for item in tools):
                return tools
        raise OpenCodeClientError("unexpected tool ids response shape", payload=data)

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
        model_ref = _model_ref_from_value(model)
        if model_ref:
            payload["model"] = model_ref
        if agent:
            payload["agent"] = agent
        if system:
            payload["system"] = system
        headers = _copilot_integration_headers_for_model(model)
        return await self._request_json("POST", f"/session/{session_id}/message", json=payload, headers=headers, expected_statuses=(200, 201))

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
            async with session.get(self._url(path), timeout=timeout) as resp:
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
            if result.get("healthy"):
                return
            await asyncio.sleep(0.5)
        raise TimeoutError(f"opencode did not become ready within {timeout_seconds} seconds")
