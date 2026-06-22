from __future__ import annotations

import json
import re
from pathlib import Path

from .thinking_events import task_lifecycle_event, utc_now_iso
from .task_store import ACTIVE_TASK_STATUSES, TaskRecord, TaskStore, is_valid_task_id


_TASK_RECORD_RECOVERY_PREFIX_BYTES = 64 * 1024


def _json_string_field_prefix(text: str, key: str) -> str | None:
    match = re.search(rf'"{re.escape(key)}"\s*:\s*("(?:\\.|[^"\\])*")', text)
    if not match:
        return None
    try:
        value = json.loads(match.group(1))
    except Exception:
        return None
    return value if isinstance(value, str) else None


def _active_record_from_task_file_prefix(path: Path) -> TaskRecord | None:
    try:
        prefix = path.open("rb").read(_TASK_RECORD_RECOVERY_PREFIX_BYTES).decode("utf-8", errors="ignore")
    except OSError:
        return None
    status = _json_string_field_prefix(prefix, "status")
    if status not in ACTIVE_TASK_STATUSES:
        return None
    task_id = _json_string_field_prefix(prefix, "task_id") or path.stem
    if not is_valid_task_id(task_id):
        return None
    return TaskRecord(
        task_id=task_id,
        task_type=_json_string_field_prefix(prefix, "task_type") or "generic_agent_task",
        request_id=_json_string_field_prefix(prefix, "request_id") or f"recovery-{task_id}",
        status=status,
        portal_session_id=_json_string_field_prefix(prefix, "portal_session_id") or f"task-{task_id}",
        opencode_session_id=_json_string_field_prefix(prefix, "opencode_session_id") or "",
        input_payload={},
        metadata={},
        output_payload={},
        artifacts={},
        runtime_events=[],
        error=None,
        created_at=_json_string_field_prefix(prefix, "created_at") or utc_now_iso(),
    )


def _blocked_after_restart_record(record: TaskRecord) -> TaskRecord:
    output_payload = record.output_payload if isinstance(record.output_payload, dict) else {}
    output_payload = dict(output_payload)
    output_payload["summary"] = "Adapter restarted before task completion"
    output_payload["error_code"] = "adapter_restarted_task_recovery_required"
    output_payload["blockers"] = ["Adapter restarted before task completion"]
    output_payload["next_recommendation"] = "Re-dispatch task if it is still required."
    runtime_events = list(record.runtime_events or [])
    runtime_events.append(
        task_lifecycle_event(
            "task.blocked",
            session_id=record.portal_session_id,
            request_id=record.request_id,
            state="blocked",
            summary="Adapter restarted before task completion",
        )
    )
    return TaskRecord(
        **{
            **record.__dict__,
            "status": "blocked",
            "output_payload": output_payload,
            "error": {
                "code": "adapter_restarted_task_recovery_required",
                "message": "Adapter restarted before task completion; manual recovery required",
            },
            "finished_at": utc_now_iso(),
            "runtime_events": runtime_events,
        }
    )


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
        task_store = TaskStore(self.state_paths.tasks_dir)
        blocked_task_ids = set()
        for record in task_store.iter_active(max_records=None, max_scan_records=None, use_default_limits=False):
            task_store.save(_blocked_after_restart_record(record))
            blocked_task_ids.add(record.task_id)
            summary["tasks_marked_blocked"] += 1
        for path in self.state_paths.tasks_dir.glob("*.json"):
            if path.stem in blocked_task_ids:
                continue
            record = _active_record_from_task_file_prefix(path)
            if record is None:
                continue
            task_store.save(_blocked_after_restart_record(record))
            blocked_task_ids.add(record.task_id)
            summary["tasks_marked_blocked"] += 1
        return summary
