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

    def start_entry(self, session_id: str, *, request_id: str, message: str, runtime_events: list[dict] | None = None, context_state: dict | None = None, llm_debug: dict | None = None) -> dict:
        d = self._ensure(session_id)
        d["entries"].append({"request_id": request_id, "status": "running", "message": safe_preview(message, 2000), "response": "", "events": [], "runtime_events": runtime_events or [], "context_state": context_state or {}, "llm_debug": llm_debug or {}, "created_at": utc_now_iso(), "finished_at": ""})
        d["updated_at"] = utc_now_iso()
        return self._write(session_id, d)

    def finish_entry(self, session_id: str, *, request_id: str, status: str, response: str, runtime_events: list[dict] | None = None, events: list[dict] | None = None, context_state: dict | None = None, llm_debug: dict | None = None) -> dict:
        d = self._ensure(session_id)
        e = self.latest_entry(session_id)
        if not e or e.get("request_id") != request_id:
            self.start_entry(session_id, request_id=request_id, message="", runtime_events=[])
            d = self._ensure(session_id)
            e = d["entries"][-1]
        e.update({"status": status, "response": safe_preview(response, 2000), "runtime_events": runtime_events or e.get("runtime_events", []), "events": events or e.get("events", []), "context_state": context_state or e.get("context_state", {}), "llm_debug": llm_debug or e.get("llm_debug", {}), "finished_at": utc_now_iso()})
        d["updated_at"] = utc_now_iso()
        return self._write(session_id, d)

    def fail_entry(self, session_id: str, *, request_id: str, error: str, runtime_events: list[dict] | None = None, context_state: dict | None = None, llm_debug: dict | None = None) -> dict:
        return self.finish_entry(session_id, request_id=request_id, status="error", response=error, runtime_events=runtime_events, events=runtime_events, context_state=context_state, llm_debug=llm_debug)

    def append_event(self, session_id: str, *, request_id: str, event: dict, runtime: bool = True) -> dict:
        d = self._ensure(session_id)
        e = self.latest_entry(session_id)
        if not e or e.get("request_id") != request_id:
            self.start_entry(session_id, request_id=request_id, message="")
            d = self._ensure(session_id)
            e = d["entries"][-1]
        key = "runtime_events" if runtime else "events"
        e.setdefault(key, []).append(event)
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
