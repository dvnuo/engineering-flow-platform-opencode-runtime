from __future__ import annotations

import json
import re
from pathlib import Path

from .thinking_events import safe_preview, utc_now_iso


class ChatLogStore:
    def __init__(self, chatlogs_dir: Path):
        self.chatlogs_dir = chatlogs_dir

    def _sanitize(self, session_id: str) -> str:
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", str(session_id)).strip(".-")
        return name or "default"

    def _path(self, session_id: str) -> Path:
        return self.chatlogs_dir / f"{self._sanitize(session_id)}.json"

    def get(self, session_id: str) -> dict | None:
        p = self._path(session_id)
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def _write(self, session_id: str, payload: dict) -> dict:
        path = self._path(session_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        return payload

    def _ensure(self, session_id: str) -> dict:
        cur = self.get(session_id)
        if cur:
            return cur
        return {"session_id": session_id, "engine": "opencode", "entries": [], "updated_at": utc_now_iso()}

    def _find_entry(self, payload: dict, request_id: str) -> dict | None:
        entries = payload.setdefault("entries", [])
        for entry in reversed(entries):
            if entry.get("request_id") == request_id:
                return entry
        return None

    def _new_entry(self, request_id: str) -> dict:
        return {
            "request_id": request_id,
            "status": "running",
            "message": "",
            "response": "",
            "events": [],
            "runtime_events": [],
            "context_state": {},
            "llm_debug": {},
            "created_at": utc_now_iso(),
            "finished_at": "",
        }

    def start_entry(self, session_id: str, *, request_id: str, message: str, runtime_events: list[dict] | None = None, context_state: dict | None = None, llm_debug: dict | None = None) -> dict:
        d = self._ensure(session_id)
        d.setdefault("entries", []).append({
            "request_id": request_id,
            "status": "running",
            "message": safe_preview(message, 2000),
            "response": "",
            "events": [],
            "runtime_events": safe_preview(runtime_events or [], 4000),
            "context_state": safe_preview(context_state or {}, 4000),
            "llm_debug": safe_preview(llm_debug or {}, 4000),
            "created_at": utc_now_iso(),
            "finished_at": "",
        })
        d["updated_at"] = utc_now_iso()
        return self._write(session_id, d)

    def finish_entry(self, session_id: str, *, request_id: str, status: str, response: str, runtime_events: list[dict] | None = None, events: list[dict] | None = None, context_state: dict | None = None, llm_debug: dict | None = None) -> dict:
        d = self._ensure(session_id)
        e = self._find_entry(d, request_id)
        if e is None:
            e = self._new_entry(request_id)
            d.setdefault("entries", []).append(e)

        e.update({
            "status": status,
            "response": safe_preview(response, 2000),
            "runtime_events": safe_preview(runtime_events if runtime_events is not None else e.get("runtime_events", []), 4000),
            "events": safe_preview(events if events is not None else e.get("events", []), 4000),
            "context_state": safe_preview(context_state if context_state is not None else e.get("context_state", {}), 4000),
            "llm_debug": safe_preview(llm_debug if llm_debug is not None else e.get("llm_debug", {}), 4000),
            "finished_at": utc_now_iso(),
        })
        d["updated_at"] = utc_now_iso()
        return self._write(session_id, d)

    def fail_entry(self, session_id: str, *, request_id: str, error: str, runtime_events: list[dict] | None = None, context_state: dict | None = None, llm_debug: dict | None = None) -> dict:
        return self.finish_entry(session_id, request_id=request_id, status="error", response=error, runtime_events=runtime_events, events=runtime_events, context_state=context_state, llm_debug=llm_debug)

    def append_event(self, session_id: str, *, request_id: str, event: dict, runtime: bool = True) -> dict:
        d = self._ensure(session_id)
        e = self._find_entry(d, request_id)
        if e is None:
            e = self._new_entry(request_id)
            d.setdefault("entries", []).append(e)
        key = "runtime_events" if runtime else "events"
        e.setdefault(key, []).append(safe_preview(event, 1000))
        d["updated_at"] = utc_now_iso()
        return self._write(session_id, d)

    def latest_entry(self, session_id: str) -> dict | None:
        d = self.get(session_id)
        if not d or not d.get("entries"):
            return None
        return d["entries"][-1]

    def list_session_ids(self) -> list[str]:
        out = []
        for p in self.chatlogs_dir.glob("*.json"):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")).get("session_id", p.stem))
            except Exception:
                out.append(p.stem)
        return sorted(set(out))
