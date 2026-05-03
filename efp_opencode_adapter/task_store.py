from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def is_valid_task_id(task_id: str) -> bool:
    return bool(task_id) and all(token not in task_id for token in ("/", "\\", ".."))


@dataclass
class TaskRecord:
    task_id: str
    task_type: str
    request_id: str
    status: str
    portal_session_id: str
    opencode_session_id: str
    input_payload: dict[str, Any]
    metadata: dict[str, Any]
    output_payload: dict[str, Any] | None
    artifacts: dict[str, Any]
    runtime_events: list[dict[str, Any]]
    error: dict[str, Any] | None
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    source: str | None = None
    shared_context_ref: str | None = None
    context_ref: Any = None
    message_cursor: int | None = None
    opencode_prompt_id: str | None = None
    opencode_message_id: str | None = None
    completion_source: str | None = None
    pending_permission_ids: list[str] | None = None


class TaskStore:
    def __init__(self, tasks_dir: Path):
        self.tasks_dir = tasks_dir
        self.tasks_dir.mkdir(parents=True, exist_ok=True)

    def _task_path(self, task_id: str) -> Path:
        if not is_valid_task_id(task_id):
            raise ValueError("invalid_task_id")
        return self.tasks_dir / f"{task_id}.json"

    def get(self, task_id: str) -> TaskRecord | None:
        path = self._task_path(task_id)
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        return TaskRecord(**payload)

    def save(self, record: TaskRecord) -> TaskRecord:
        path = self._task_path(record.task_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(record), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        return record

    def create_or_get(self, record: TaskRecord) -> tuple[TaskRecord, bool]:
        existing = self.get(record.task_id)
        if existing is not None:
            return existing, False
        return self.save(record), True

    def update(self, task_id: str, **changes: Any) -> TaskRecord:
        record = self.get(task_id)
        if record is None:
            raise KeyError(task_id)
        data = asdict(record)
        data.update(changes)
        updated = TaskRecord(**data)
        return self.save(updated)

    def append_event(self, task_id: str, event: dict[str, Any]) -> TaskRecord:
        record = self.get(task_id)
        if record is None:
            raise KeyError(task_id)
        events = list(record.runtime_events)
        events.append(event)
        return self.update(task_id, runtime_events=events)
