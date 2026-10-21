from __future__ import annotations

import json
import logging
from pathlib import Path
from urllib.parse import quote

import aiohttp

from .settings import Settings
from .thinking_events import safe_preview, utc_now_iso

logger = logging.getLogger(__name__)


class PortalMetadataClient:
    def __init__(self, settings: Settings, pending_file: Path | None = None, session: aiohttp.ClientSession | None = None):
        self.settings = settings
        self.pending_file = pending_file
        self._session = session

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.settings.portal_internal_token:
            headers["Authorization"] = f"Bearer {self.settings.portal_internal_token}"
            headers["X-Portal-Internal-Token"] = self.settings.portal_internal_token
        return headers

    def _session_url(self, session_id: str, leaf: str) -> str:
        base_url = (self.settings.portal_internal_base_url or "").rstrip("/")
        agent = quote(self.settings.portal_agent_id or "", safe="")
        sid = quote(session_id, safe="")
        return f"{base_url}/api/internal/agents/{agent}/sessions/{sid}/{leaf}"

    def _metadata_url(self, session_id: str) -> str:
        return self._session_url(session_id, "metadata")

    def _title_url(self, session_id: str) -> str:
        return self._session_url(session_id, "title")

    def _timeout(self) -> aiohttp.ClientTimeout:
        return aiohttp.ClientTimeout(total=self.settings.portal_metadata_timeout_seconds)

    def _record_pending(self, *, method: str, url: str, payload: dict, error: str) -> None:
        """Append a failed publish to the pending file.

        Nothing replays this file today; it is the operator's record of what
        the Portal did not receive. A session name that fails to publish
        reaches the Portal with the next full metadata publish anyway.
        """
        if not self.pending_file:
            return
        try:
            self.pending_file.parent.mkdir(parents=True, exist_ok=True)
            clean = {"method": method, "url": url, "payload": safe_preview(payload, 4000), "error": safe_preview(error, 1000)}
            with self.pending_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(clean, ensure_ascii=False) + "\n")
        except Exception:
            logger.debug("portal metadata pending file could not be written", exc_info=True)

    async def _parse_delete_response(self, resp) -> dict:
        payload = None
        try:
            payload = await resp.json()
        except Exception:
            payload = None
        if 200 <= resp.status < 300:
            return {"success": True, "status": resp.status, "payload": payload}
        if resp.status in {404, 405}:
            return {"success": False, "skipped": True, "status": resp.status, "reason": "portal_metadata_delete_endpoint_unavailable"}
        err = await resp.text()
        return {"success": False, "status": resp.status, "error": safe_preview(err)}

    async def _parse_title_response(self, resp) -> dict:
        if 200 <= resp.status < 300:
            return {"success": True, "status": resp.status}
        if resp.status in {404, 405}:
            # A Portal without the title endpoint: the name arrives with the
            # next full publish (session_title in the metadata).
            return {"success": False, "skipped": True, "status": resp.status, "reason": "portal_title_endpoint_unavailable"}
        err = await resp.text()
        return {"success": False, "status": resp.status, "error": safe_preview(err)}

    async def publish_session_metadata(self, *, session_id: str, latest_event_type: str, latest_event_state: str, request_id: str | None = None, task_id: str | None = None, summary: str | None = None, runtime_events: list[dict] | None = None, metadata: dict | None = None) -> dict:
        if not self.settings.portal_internal_base_url or not self.settings.portal_agent_id:
            return {"success": False, "skipped": True}
        runtime_events = runtime_events or []
        md = dict(metadata or {})
        safe_runtime_events = safe_preview(runtime_events[-50:], 4000)
        safe_metadata = safe_preview({**md, "latest_summary": summary, "engine": "opencode", "updated_at": utc_now_iso()}, 2000)
        payload = {"latest_event_type": latest_event_type, "latest_event_state": latest_event_state, "last_execution_id": request_id, "current_task_id": task_id or None, "runtime_events_json": json.dumps(safe_runtime_events, ensure_ascii=False), "metadata_json": json.dumps(safe_metadata, ensure_ascii=False)}
        url = self._metadata_url(session_id)
        headers = self._headers()
        try:
            if self._session is not None:
                resp = await self._session.put(url, json=payload, headers=headers, timeout=self._timeout())
                async with resp:
                    if 200 <= resp.status < 300:
                        return {"success": True, "status": resp.status}
                    err = await resp.text()
                    raise RuntimeError(f"status={resp.status} {err}")
            async with aiohttp.ClientSession() as s:
                async with s.put(url, json=payload, headers=headers, timeout=self._timeout()) as resp:
                    if 200 <= resp.status < 300:
                        return {"success": True, "status": resp.status}
                    err = await resp.text()
                    raise RuntimeError(f"status={resp.status} {err}")
        except Exception as exc:
            self._record_pending(method="PUT", url=url, payload=payload, error=str(exc))
            return {"success": False, "error": safe_preview(str(exc))}

    async def publish_session_title(self, *, session_id: str, title: str) -> dict:
        """Tell the Portal the session's name changed (generated after the first turn, regenerated, or renamed).

        A PATCH of the title alone, best effort: the full publish replaces
        every column of the Portal's row, and a generated title arrives in the
        background with no run state to send alongside it. A Portal without
        the endpoint (404/405) is skipped the way ``delete_session_metadata``
        skips; any other failure is logged and recorded in the pending file.
        Either way the name reaches the Portal with the next full publish.
        """
        normalized_title = str(title or "").strip()
        if not self.settings.portal_internal_base_url or not self.settings.portal_agent_id or not normalized_title:
            return {"success": False, "skipped": True}
        url = self._title_url(session_id)
        headers = self._headers()
        payload = {"title": normalized_title}
        try:
            if self._session is not None:
                resp = await self._session.patch(url, json=payload, headers=headers, timeout=self._timeout())
                try:
                    out = await self._parse_title_response(resp)
                finally:
                    resp.release()
            else:
                async with aiohttp.ClientSession() as s:
                    async with s.patch(url, json=payload, headers=headers, timeout=self._timeout()) as resp:
                        out = await self._parse_title_response(resp)
        except Exception as exc:
            out = {"success": False, "error": safe_preview(str(exc))}
        if not out.get("success") and not out.get("skipped"):
            logger.warning("Session title publish failed for session %s: %s", session_id, out.get("error") or out.get("status"))
            self._record_pending(method="PATCH", url=url, payload=payload, error=str(out.get("error") or out.get("status")))
        return out

    async def delete_session_metadata(self, session_id: str) -> dict:
        if not self.settings.portal_internal_base_url or not self.settings.portal_agent_id:
            return {"success": False, "skipped": True, "reason": "portal_metadata_not_configured"}
        url = self._metadata_url(session_id)
        headers = self._headers()
        timeout = self._timeout()
        try:
            if self._session is not None:
                resp = await self._session.delete(url, headers=headers, timeout=timeout)
                try:
                    return await self._parse_delete_response(resp)
                finally:
                    resp.release()
            async with aiohttp.ClientSession() as s:
                async with s.delete(url, headers=headers, timeout=timeout) as resp:
                    return await self._parse_delete_response(resp)
        except Exception as exc:
            return {"success": False, "error": safe_preview(str(exc))}
