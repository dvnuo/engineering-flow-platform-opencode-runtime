from __future__ import annotations

import json
from pathlib import Path

from .thinking_events import task_lifecycle_event, utc_now_iso


class RecoveryManager:
    def __init__(self, *, settings, state_paths, session_store, chatlog_store, opencode_client):
        self.settings = settings
        self.state_paths = state_paths
        self.session_store = session_store
        self.chatlog_store = chatlog_store
        self.opencode_client = opencode_client

    async def recover(self) -> dict:
        summary = {"sessions_reloaded": 0, "partial_recovery_marked": 0, "tasks_marked_blocked": 0, "corrupted_chatlogs": 0, "opencode_errors": 0}
        self.session_store.reload()
        summary["sessions_reloaded"] = len(self.session_store.list_active())
        for p in self.state_paths.chatlogs_dir.glob("*.json"):
            try:
                json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                summary["corrupted_chatlogs"] += 1
        for rec in self.session_store.list_active():
            try:
                await self.opencode_client.get_session(rec.opencode_session_id)
            except Exception as exc:
                if getattr(exc, "status", None) == 404:
                    self.session_store.upsert(type(rec)(**{**rec.__dict__, "partial_recovery": True, "updated_at": utc_now_iso()}))
                    summary["partial_recovery_marked"] += 1
                else:
                    summary["opencode_errors"] += 1
        tasks_dir: Path = self.state_paths.tasks_dir
        for p in tasks_dir.glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if data.get("status") in {"accepted", "running"}:
                data["status"] = "blocked"
                data["error_code"] = "adapter_restarted_task_recovery_required"
                data["error"] = {"code": "adapter_restarted_task_recovery_required", "message": "Adapter restarted before task completion; manual recovery required"}
                data["finished_at"] = utc_now_iso()
                ev = data.get("runtime_events") if isinstance(data.get("runtime_events"), list) else []
                ev.append(task_lifecycle_event("task.blocked", session_id=str(data.get("session_id", "")), request_id=str(data.get("request_id", "")), state="blocked", summary="Adapter restarted before task completion"))
                data["runtime_events"] = ev
                p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                summary["tasks_marked_blocked"] += 1
        return summary
