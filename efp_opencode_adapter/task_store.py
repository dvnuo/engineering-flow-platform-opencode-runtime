from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

UTC = timezone.utc
ACTIVE_TASK_STATUSES = frozenset({"accepted", "running"})
DEFAULT_TASK_LIST_MAX_RECORDS = 512
DEFAULT_TASK_SCAN_MAX_RECORDS = 1024
DEFAULT_TASK_LOAD_MAX_FILE_BYTES = 2_000_000
DEFAULT_TASK_PERSIST_MAX_FILE_BYTES = 2_000_000
DEFAULT_TASK_PERSIST_EVENT_TAIL = 50


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
        try:
            return self._read_record(path)
        except Exception:
            return None

    def save(self, record: TaskRecord) -> TaskRecord:
        path = self._task_path(record.task_id)
        tmp = path.with_suffix(".json.tmp")
        encoded = _encode_record_for_persistence(record)
        if encoded is None:
            tmp.unlink(missing_ok=True)
            if record.status not in ACTIVE_TASK_STATUSES:
                path.unlink(missing_ok=True)
            return record
        tmp.write_text(encoded, encoding="utf-8")
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

    def list_all(
        self,
        *,
        max_records: int | None = None,
        max_scan_records: int | None = None,
        max_file_bytes: int | None = None,
    ) -> list[TaskRecord]:
        limits = task_store_limits()
        record_limit = _coerce_non_negative_int(
            max_records if max_records is not None else limits["list_max_records"]
        )
        scan_limit = _coerce_non_negative_int(
            max_scan_records if max_scan_records is not None else limits["scan_max_records"]
        )
        file_size_limit = _coerce_non_negative_int(
            max_file_bytes if max_file_bytes is not None else limits["load_max_file_bytes"]
        )
        if record_limit == 0 or scan_limit == 0:
            return []
        records = []
        scanned = 0
        for path in sorted(self.tasks_dir.glob("*.json")):
            if record_limit is not None and len(records) >= record_limit:
                break
            if scan_limit is not None and scanned >= scan_limit:
                break
            scanned += 1
            if file_size_limit is not None:
                try:
                    if path.stat().st_size > file_size_limit:
                        continue
                except OSError:
                    continue
            try:
                records.append(self._read_record(path, max_file_bytes=file_size_limit))
            except Exception:
                continue
        return records

    def list_active(
        self,
        *,
        max_records: int | None = None,
        max_scan_records: int | None = None,
        max_file_bytes: int | None = None,
    ) -> list[TaskRecord]:
        limits = task_store_limits()
        record_limit = _coerce_non_negative_int(
            max_records if max_records is not None else limits["list_max_records"]
        )
        scan_limit = _coerce_non_negative_int(
            max_scan_records if max_scan_records is not None else limits["scan_max_records"]
        )
        file_size_limit = _coerce_non_negative_int(
            max_file_bytes if max_file_bytes is not None else limits["load_max_file_bytes"]
        )
        if record_limit == 0 or scan_limit == 0:
            return []
        records = []
        scanned = 0
        for path in sorted(self.tasks_dir.glob("*.json")):
            if record_limit is not None and len(records) >= record_limit:
                break
            if scan_limit is not None and scanned >= scan_limit:
                break
            scanned += 1
            if file_size_limit is not None:
                try:
                    if path.stat().st_size > file_size_limit:
                        continue
                except OSError:
                    continue
            try:
                record = self._read_record(path, max_file_bytes=file_size_limit)
            except Exception:
                continue
            if record.status in ACTIVE_TASK_STATUSES:
                records.append(record)
        return records

    def find_for_opencode_event(self, opencode_session_id: str, message_ids: set[str]) -> TaskRecord | None:
        if not opencode_session_id:
            return None
        matched = []
        for record in self.list_all():
            if record.opencode_session_id != opencode_session_id:
                continue
            if message_ids and (
                record.opencode_message_id in message_ids or record.opencode_prompt_id in message_ids
            ):
                return record
            if record.status in ACTIVE_TASK_STATUSES:
                matched.append(record)
        if not message_ids and len(matched) == 1:
            return matched[0]
        return None

    def _read_record(self, path: Path, *, max_file_bytes: int | None = None) -> TaskRecord:
        file_size_limit = _coerce_non_negative_int(
            max_file_bytes if max_file_bytes is not None else task_store_limits()["load_max_file_bytes"]
        )
        if file_size_limit is not None and path.stat().st_size > file_size_limit:
            raise ValueError("task_record_exceeds_load_limit")
        payload = json.loads(path.read_text(encoding="utf-8"))
        return TaskRecord(**payload)


def task_store_limits() -> dict[str, int]:
    return {
        "list_max_records": _env_non_negative_int(
            "EFP_OPENCODE_TASKS_LIST_MAX_RECORDS",
            DEFAULT_TASK_LIST_MAX_RECORDS,
        ),
        "scan_max_records": _env_non_negative_int(
            "EFP_OPENCODE_TASKS_SCAN_MAX_RECORDS",
            DEFAULT_TASK_SCAN_MAX_RECORDS,
        ),
        "load_max_file_bytes": _env_non_negative_int(
            "EFP_OPENCODE_TASKS_LOAD_MAX_FILE_BYTES",
            DEFAULT_TASK_LOAD_MAX_FILE_BYTES,
        ),
        "persist_max_file_bytes": _env_positive_int(
            "EFP_OPENCODE_TASKS_PERSIST_MAX_FILE_BYTES",
            DEFAULT_TASK_PERSIST_MAX_FILE_BYTES,
        ),
        "persist_event_tail": _env_non_negative_int(
            "EFP_OPENCODE_TASKS_PERSIST_EVENT_TAIL",
            DEFAULT_TASK_PERSIST_EVENT_TAIL,
        ),
    }


def _env_non_negative_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return max(0, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return max(0, default)
    return max(0, value)


def _env_positive_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return max(1, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return max(1, default)
    return max(1, value)


def _coerce_non_negative_int(value: int | None) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _encode_record_for_persistence(record: TaskRecord) -> str | None:
    max_bytes = task_store_limits()["persist_max_file_bytes"]
    encoded = _json_dumps(asdict(record))
    if len(encoded.encode("utf-8")) <= max_bytes:
        return encoded
    encoded = _json_dumps(_minimal_record_for_persistence(record, keep_event_tail=True))
    if len(encoded.encode("utf-8")) <= max_bytes:
        return encoded
    encoded = _json_dumps(_minimal_record_for_persistence(record, keep_event_tail=False))
    if len(encoded.encode("utf-8")) <= max_bytes:
        return encoded
    encoded = _json_dumps(_ultra_minimal_record_for_persistence(record))
    if len(encoded.encode("utf-8")) <= max_bytes:
        return encoded
    return None


def _minimal_record_for_persistence(record: TaskRecord, *, keep_event_tail: bool) -> dict[str, Any]:
    limits = task_store_limits()
    payload = _minimal_output_payload(record)
    runtime_events = list(record.runtime_events or [])
    if keep_event_tail and limits["persist_event_tail"] > 0:
        runtime_events = runtime_events[-limits["persist_event_tail"] :]
    else:
        runtime_events = []
    metadata = _minimal_metadata(record.metadata)
    return {
        "task_id": record.task_id,
        "task_type": record.task_type,
        "request_id": record.request_id,
        "status": record.status,
        "portal_session_id": record.portal_session_id,
        "opencode_session_id": record.opencode_session_id,
        "input_payload": {
            "_omitted": True,
            "reason": "task_record_exceeded_persistence_limit",
        }
        if record.input_payload
        else {},
        "metadata": metadata,
        "output_payload": payload,
        "artifacts": {},
        "runtime_events": runtime_events,
        "error": record.error,
        "created_at": record.created_at,
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "source": record.source,
        "shared_context_ref": record.shared_context_ref,
        "context_ref": None,
        "message_cursor": record.message_cursor,
        "opencode_prompt_id": record.opencode_prompt_id,
        "opencode_message_id": record.opencode_message_id,
        "completion_source": record.completion_source,
        "pending_permission_ids": record.pending_permission_ids,
    }


def _minimal_output_payload(record: TaskRecord) -> dict[str, Any]:
    payload = {
        "status": record.status,
        "payload_omitted_from_persistence": True,
        "reason": "task_record_exceeded_persistence_limit",
    }
    source = record.output_payload if isinstance(record.output_payload, dict) else {}
    for key in (
        "summary",
        "error_code",
        "blockers",
        "next_recommendation",
        "completion_state",
        "incomplete_reason",
        "pending_permission_ids",
    ):
        if key in source:
            payload[key] = source[key]
    return payload


def _ultra_minimal_record_for_persistence(record: TaskRecord) -> dict[str, Any]:
    return {
        "task_id": record.task_id,
        "task_type": record.task_type,
        "request_id": record.request_id,
        "status": record.status,
        "portal_session_id": record.portal_session_id,
        "opencode_session_id": record.opencode_session_id,
        "input_payload": {},
        "metadata": {},
        "output_payload": {
            "status": record.status,
            "payload_omitted_from_persistence": True,
            "record_minimized_from_persistence": True,
            "reason": "task_record_exceeded_persistence_limit",
        },
        "artifacts": {},
        "runtime_events": [],
        "error": record.error,
        "created_at": record.created_at,
    }


def _minimal_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    keep = {}
    for key in (
        "task_id",
        "portal_task_id",
        "agent_id",
        "trace_id",
        "portal_dispatch_id",
        "runtime_request_id",
        "opencode_session_id",
    ):
        if key in metadata:
            keep[key] = metadata[key]
    return keep
