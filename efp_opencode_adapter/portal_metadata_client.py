from __future__ import annotations

import json
from pathlib import Path

import aiohttp

from .settings import Settings
from .thinking_events import safe_preview, utc_now_iso


class PortalMetadataClient:
    def __init__(self, settings: Settings, pending_file: Path | None = None, session: aiohttp.ClientSession | None = None):
        self.settings = settings
        self.pending_file = pending_file
        self._session = session

    async def publish_session_metadata(self, *, session_id: str, latest_event_type: str, latest_event_state: str, request_id: str | None = None, task_id: str | None = None, summary: str | None = None, runtime_events: list[dict] | None = None, metadata: dict | None = None) -> dict:
        if not self.settings.portal_internal_base_url or not self.settings.portal_agent_id:
            return {"success": False, "skipped": True}
        runtime_events = runtime_events or []
        md = dict(metadata or {})
        safe_metadata = safe_preview({**md, "latest_summary": summary, "engine": "opencode", "updated_at": utc_now_iso()}, 2000)
        payload = {"latest_event_type": latest_event_type, "latest_event_state": latest_event_state, "last_execution_id": request_id, "current_task_id": task_id or None, "runtime_events_json": json.dumps(runtime_events[-50:], ensure_ascii=False), "metadata_json": json.dumps(safe_metadata, ensure_ascii=False)}
        url = f"{self.settings.portal_internal_base_url.rstrip('/')}/api/internal/agents/{self.settings.portal_agent_id}/sessions/{session_id}/metadata"
        headers = {}
        if self.settings.portal_internal_token:
            headers["Authorization"] = f"Bearer {self.settings.portal_internal_token}"
            headers["X-Portal-Internal-Token"] = self.settings.portal_internal_token
        try:
            if self._session is not None:
                resp = await self._session.put(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=self.settings.portal_metadata_timeout_seconds))
                async with resp:
                    if 200 <= resp.status < 300:
                        return {"success": True, "status": resp.status}
                    err = await resp.text()
                    raise RuntimeError(f"status={resp.status} {err}")
            async with aiohttp.ClientSession() as s:
                async with s.put(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=self.settings.portal_metadata_timeout_seconds)) as resp:
                    if 200 <= resp.status < 300:
                        return {"success": True, "status": resp.status}
                    err = await resp.text()
                    raise RuntimeError(f"status={resp.status} {err}")
        except Exception as exc:
            if self.pending_file:
                self.pending_file.parent.mkdir(parents=True, exist_ok=True)
                clean = {"url": url, "payload": safe_preview(payload, 4000), "error": safe_preview(str(exc), 1000)}
                with self.pending_file.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(clean, ensure_ascii=False) + "\n")
            return {"success": False, "error": safe_preview(str(exc))}
