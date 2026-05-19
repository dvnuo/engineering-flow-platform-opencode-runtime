from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .thinking_events import safe_preview, utc_now_iso


ACTIVE_RUN_STATUSES = {"accepted", "running", "recovering", "stream_attached", "stream_detached"}
TERMINAL_RUN_STATUSES = {"completed", "incomplete", "failed", "cancelled"}
ALLOWED_RUN_STATUSES = ACTIVE_RUN_STATUSES | TERMINAL_RUN_STATUSES
ALLOWED_STREAM_STATES = {"none", "attached", "detached", "closed"}


@dataclass
class ChatRunRecord:
    request_id: str
    portal_session_id: str
    opencode_session_id: str
    user_message_id: str = ""
    assistant_message_id: str = ""
    assistant_message_ids: list[str] = field(default_factory=list)
    status: str = "accepted"
    stream_state: str = "none"
    completion_state: str = ""
    incomplete_reason: str = ""
    final_payload: dict[str, Any] = field(default_factory=dict)
    last_response_text: str = ""
    last_display_blocks: list[Any] = field(default_factory=list)
    last_event_at: str = ""
    started_at: str = ""
    updated_at: str = ""
    completed_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def _coerce_status(value: Any, default: str = "running") -> str:
    status = str(value or "").strip()
    return status if status in ALLOWED_RUN_STATUSES else default


def _coerce_stream_state(value: Any, default: str = "none") -> str:
    state = str(value or "").strip()
    return state if state in ALLOWED_STREAM_STATES else default


def _safe_dict(value: Any, limit: int = 4000) -> dict[str, Any]:
    safe = safe_preview(value or {}, limit)
    return safe if isinstance(safe, dict) else {}


def _safe_list(value: Any, limit: int = 4000) -> list[Any]:
    safe = safe_preview(value or [], limit)
    return safe if isinstance(safe, list) else []


def _safe_text(value: Any, limit: int = 12000) -> str:
    if not isinstance(value, str):
        value = "" if value is None else str(value)
    safe = safe_preview(value, limit)
    return safe if isinstance(safe, str) else ""


def _append_unique(target: list[str], values: list[Any] | tuple[Any, ...] | set[Any]) -> list[str]:
    out = [str(item) for item in target if item]
    seen = set(out)
    for value in values:
        if not value:
            continue
        item = str(value)
        if item in seen:
            continue
        out.append(item)
        seen.add(item)
    return out


def _event_type(event: dict[str, Any]) -> str:
    return str(event.get("type") or event.get("event_type") or "")


def _event_data(event: dict[str, Any]) -> dict[str, Any]:
    data = event.get("data")
    return data if isinstance(data, dict) else {}


def _event_delta_text(event: dict[str, Any]) -> str:
    data = _event_data(event)
    event_type = _event_type(event)
    role = str(data.get("message_role") or data.get("role") or data.get("source_role") or event.get("role") or "").lower()
    part_type = str(data.get("part_type") or event.get("part_type") or "").lower()
    if part_type in {"reasoning", "thinking", "chain_of_thought", "hidden"}:
        return ""
    if event_type == "message.delta" and role and role != "assistant":
        return ""
    for key in ("delta", "text", "content", "message"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return _safe_text(value, 1000)
    for key in ("delta", "text", "content", "message"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return _safe_text(value, 1000)
    return ""


class ChatRunStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._runs: dict[str, ChatRunRecord] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self._runs = {}
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            self._runs = {}
            return
        runs = payload.get("runs") if isinstance(payload, dict) else {}
        if not isinstance(runs, dict):
            self._runs = {}
            return
        loaded: dict[str, ChatRunRecord] = {}
        for request_id, raw in runs.items():
            if not isinstance(raw, dict):
                continue
            try:
                record = ChatRunRecord(
                    request_id=str(raw.get("request_id") or request_id),
                    portal_session_id=str(raw.get("portal_session_id") or raw.get("session_id") or ""),
                    opencode_session_id=str(raw.get("opencode_session_id") or ""),
                    user_message_id=str(raw.get("user_message_id") or ""),
                    assistant_message_id=str(raw.get("assistant_message_id") or ""),
                    assistant_message_ids=[str(item) for item in raw.get("assistant_message_ids", []) if item] if isinstance(raw.get("assistant_message_ids"), list) else [],
                    status=_coerce_status(raw.get("status"), default="running"),
                    stream_state=_coerce_stream_state(raw.get("stream_state")),
                    completion_state=str(raw.get("completion_state") or ""),
                    incomplete_reason=str(raw.get("incomplete_reason") or ""),
                    final_payload=_safe_dict(raw.get("final_payload"), 8000),
                    last_response_text=_safe_text(raw.get("last_response_text")),
                    last_display_blocks=_safe_list(raw.get("last_display_blocks")),
                    last_event_at=str(raw.get("last_event_at") or ""),
                    started_at=str(raw.get("started_at") or utc_now_iso()),
                    updated_at=str(raw.get("updated_at") or utc_now_iso()),
                    completed_at=str(raw.get("completed_at")) if raw.get("completed_at") else None,
                    metadata=_safe_dict(raw.get("metadata"), 4000),
                )
            except Exception:
                continue
            loaded[record.request_id] = record
        self._runs = loaded

    reload = load

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"runs": {request_id: asdict(record) for request_id, record in self._runs.items()}}
        tmp_path = self.path.with_name(f"{self.path.name}.tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self.path)
        try:
            dir_fd = os.open(str(self.path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except Exception:
            pass

    def _save(self, record: ChatRunRecord) -> ChatRunRecord:
        record.updated_at = utc_now_iso()
        record.status = _coerce_status(record.status)
        record.stream_state = _coerce_stream_state(record.stream_state)
        record.final_payload = _safe_dict(record.final_payload, 8000)
        record.metadata = _safe_dict(record.metadata, 4000)
        record.last_display_blocks = _safe_list(record.last_display_blocks)
        record.last_response_text = _safe_text(record.last_response_text)
        record.assistant_message_ids = _append_unique([], record.assistant_message_ids)
        if record.assistant_message_id:
            record.assistant_message_ids = _append_unique(record.assistant_message_ids, [record.assistant_message_id])
        self._runs[record.request_id] = record
        self._write()
        return record

    def start_run(
        self,
        *,
        request_id: str,
        portal_session_id: str,
        opencode_session_id: str = "",
        user_message_id: str = "",
        assistant_message_id: str = "",
        assistant_message_ids: list[str] | None = None,
        status: str = "accepted",
        stream_state: str = "none",
        completion_state: str = "running",
        incomplete_reason: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> ChatRunRecord:
        now = utc_now_iso()
        existing = self._runs.get(request_id)
        if existing is None:
            record = ChatRunRecord(
                request_id=request_id,
                portal_session_id=portal_session_id,
                opencode_session_id=opencode_session_id,
                user_message_id=user_message_id,
                assistant_message_id=assistant_message_id,
                assistant_message_ids=_append_unique([], assistant_message_ids or []),
                status=_coerce_status(status, default="accepted"),
                stream_state=_coerce_stream_state(stream_state),
                completion_state=completion_state,
                incomplete_reason=incomplete_reason,
                started_at=now,
                updated_at=now,
                metadata=_safe_dict(metadata or {}),
            )
        else:
            record = existing
            record.portal_session_id = portal_session_id or record.portal_session_id
            record.opencode_session_id = opencode_session_id or record.opencode_session_id
            record.user_message_id = user_message_id or record.user_message_id
            record.assistant_message_id = assistant_message_id or record.assistant_message_id
            record.assistant_message_ids = _append_unique(record.assistant_message_ids, assistant_message_ids or [])
            if record.status not in TERMINAL_RUN_STATUSES:
                record.status = _coerce_status(status, default=record.status or "running")
            if stream_state != "none" or record.stream_state == "none":
                record.stream_state = _coerce_stream_state(stream_state, default=record.stream_state or "none")
            record.completion_state = completion_state or record.completion_state
            record.incomplete_reason = incomplete_reason or record.incomplete_reason
            record.metadata = _safe_dict({**record.metadata, **(metadata or {})})
        return self._save(record)

    def attach_stream(self, request_id: str) -> ChatRunRecord | None:
        record = self._runs.get(request_id)
        if record is None:
            return None
        if record.status not in TERMINAL_RUN_STATUSES:
            record.status = "stream_attached"
        record.stream_state = "attached"
        record.last_event_at = utc_now_iso()
        return self._save(record)

    def detach_stream(self, request_id: str, reason: str, metadata: dict[str, Any] | None = None) -> ChatRunRecord | None:
        record = self._runs.get(request_id)
        if record is None:
            return None
        if record.status not in TERMINAL_RUN_STATUSES:
            record.status = "stream_detached"
        record.stream_state = "detached"
        record.incomplete_reason = record.incomplete_reason or _safe_text(reason, 300)
        record.metadata = _safe_dict({**record.metadata, "stream_detach_reason": reason, **(metadata or {})})
        record.last_event_at = utc_now_iso()
        return self._save(record)

    def complete_run(self, request_id: str, final_payload: dict[str, Any]) -> ChatRunRecord | None:
        record = self._runs.get(request_id)
        if record is None:
            return None
        record.status = "completed"
        record.stream_state = "closed"
        record.completion_state = str(final_payload.get("completion_state") or "completed") if isinstance(final_payload, dict) else "completed"
        record.incomplete_reason = ""
        record.final_payload = _safe_dict(final_payload, 12000)
        record.completed_at = utc_now_iso()
        self.update_assistant_projection(
            request_id,
            text=(final_payload.get("response") if isinstance(final_payload, dict) else "") or "",
            assistant_message_id=(final_payload.get("assistant_message_id") if isinstance(final_payload, dict) else "") or "",
            message_ids=(final_payload.get("assistant_message_ids") if isinstance(final_payload, dict) and isinstance(final_payload.get("assistant_message_ids"), list) else []),
            display_blocks=(final_payload.get("display_blocks") if isinstance(final_payload, dict) else []) or [],
            save=False,
        )
        return self._save(record)

    def mark_incomplete(self, request_id: str, reason: str, payload: dict[str, Any]) -> ChatRunRecord | None:
        record = self._runs.get(request_id)
        if record is None:
            return None
        record.status = "incomplete"
        record.stream_state = "closed" if record.stream_state == "attached" else record.stream_state
        record.completion_state = str(payload.get("completion_state") or "incomplete") if isinstance(payload, dict) else "incomplete"
        record.incomplete_reason = _safe_text(reason or (payload.get("incomplete_reason") if isinstance(payload, dict) else "") or "incomplete", 500)
        record.final_payload = _safe_dict(payload, 12000)
        record.completed_at = utc_now_iso()
        self.update_assistant_projection(
            request_id,
            text=(payload.get("response") if isinstance(payload, dict) else "") or "",
            assistant_message_id=(payload.get("assistant_message_id") if isinstance(payload, dict) else "") or "",
            message_ids=(payload.get("assistant_message_ids") if isinstance(payload, dict) and isinstance(payload.get("assistant_message_ids"), list) else []),
            save=False,
        )
        return self._save(record)

    def mark_failed(self, request_id: str, reason: str, payload: dict[str, Any]) -> ChatRunRecord | None:
        record = self._runs.get(request_id)
        if record is None:
            return None
        record.status = "failed"
        record.stream_state = "closed" if record.stream_state == "attached" else record.stream_state
        record.completion_state = str(payload.get("completion_state") or "error") if isinstance(payload, dict) else "error"
        record.incomplete_reason = _safe_text(reason or (payload.get("incomplete_reason") if isinstance(payload, dict) else "") or "failed", 500)
        record.final_payload = _safe_dict(payload, 12000)
        record.completed_at = utc_now_iso()
        return self._save(record)

    def keep_running(self, request_id: str, *, reason: str, payload: dict[str, Any] | None = None, stream_detached: bool = False) -> ChatRunRecord | None:
        record = self._runs.get(request_id)
        if record is None:
            return None
        if record.status not in TERMINAL_RUN_STATUSES:
            record.status = "stream_detached" if stream_detached or record.stream_state == "detached" else "running"
        if stream_detached:
            record.stream_state = "detached"
        record.completion_state = str((payload or {}).get("completion_state") or record.completion_state or "incomplete")
        record.incomplete_reason = _safe_text(reason, 500)
        record.metadata = _safe_dict({**record.metadata, "opencode_may_still_be_running": True})
        if payload:
            record.final_payload = _safe_dict(payload, 12000)
        return self._save(record)

    def record_transport_error(self, request_id: str, error_payload: dict[str, Any]) -> ChatRunRecord | None:
        record = self._runs.get(request_id)
        if record is None:
            return None
        safe_error = _safe_dict(error_payload, 2000)
        record.metadata = _safe_dict(
            {
                **record.metadata,
                "last_transport_error": safe_error,
                "opencode_disconnected": True,
            },
            4000,
        )
        return self._save(record)

    def mark_recovering(self, request_id: str, reason: str, metadata: dict[str, Any] | None = None) -> ChatRunRecord | None:
        record = self._runs.get(request_id)
        if record is None:
            return None
        safe_metadata = _safe_dict(metadata or {}, 4000)
        was_stream_detached = record.status == "stream_detached" and record.stream_state == "detached"
        if record.status not in TERMINAL_RUN_STATUSES:
            record.status = "stream_detached" if was_stream_detached else "recovering"
        record.stream_state = "detached"
        record.completion_state = "incomplete"
        record.incomplete_reason = _safe_text(reason or "recovering", 500)
        record.metadata = _safe_dict(
            {
                **record.metadata,
                "recovery_state": safe_metadata.get("recovery_state") or "recovering",
                **safe_metadata,
            },
            4000,
        )
        return self._save(record)

    def update_from_runtime_event(self, request_id: str, event: dict[str, Any]) -> ChatRunRecord | None:
        record = self._runs.get(request_id)
        if record is None or not isinstance(event, dict):
            return record
        event_type = _event_type(event)
        created_at = str(event.get("created_at") or utc_now_iso())
        record.last_event_at = created_at
        if record.status in {"accepted", "stream_attached"} and event_type not in {"chat.stream_detached"}:
            record.status = "stream_attached" if record.stream_state == "attached" else "running"
        if event_type in {"message.delta", "assistant_delta", "assistant.message.updated"}:
            data = _event_data(event)
            delta = _event_delta_text(event)
            text = data.get("text") if isinstance(data.get("text"), str) else ""
            message_id = str(data.get("assistant_message_id") or data.get("message_id") or event.get("message_id") or "")
            self.update_assistant_projection(
                request_id,
                text=text or delta,
                assistant_message_id=message_id,
                append_text=bool(delta and not text),
                save=False,
            )
        elif event_type == "assistant.message.completed":
            data = _event_data(event)
            self.update_assistant_projection(
                request_id,
                text=str(data.get("text") or ""),
                assistant_message_id=str(data.get("assistant_message_id") or ""),
                message_ids=data.get("assistant_message_ids") if isinstance(data.get("assistant_message_ids"), list) else [],
                display_blocks=data.get("display_blocks") if isinstance(data.get("display_blocks"), list) else [],
                save=False,
            )
        return self._save(record)

    def update_assistant_projection(
        self,
        request_id: str,
        *,
        text: str | None = None,
        assistant_message_id: str = "",
        message_ids: list[Any] | None = None,
        display_blocks: list[Any] | None = None,
        append_text: bool = False,
        save: bool = True,
    ) -> ChatRunRecord | None:
        record = self._runs.get(request_id)
        if record is None:
            return None
        if text is not None:
            safe_text = _safe_text(text)
            record.last_response_text = _safe_text(f"{record.last_response_text}{safe_text}" if append_text else safe_text)
        if assistant_message_id:
            record.assistant_message_id = str(assistant_message_id)
            record.assistant_message_ids = _append_unique(record.assistant_message_ids, [assistant_message_id])
        if message_ids:
            record.assistant_message_ids = _append_unique(record.assistant_message_ids, message_ids)
            if not record.assistant_message_id and record.assistant_message_ids:
                record.assistant_message_id = record.assistant_message_ids[-1]
        if display_blocks is not None:
            record.last_display_blocks = _safe_list(display_blocks)
        if save:
            return self._save(record)
        return record

    def get(self, request_id: str) -> ChatRunRecord | None:
        return self._runs.get(request_id)

    def latest_for_session(self, portal_session_id: str) -> ChatRunRecord | None:
        runs = [record for record in self._runs.values() if record.portal_session_id == portal_session_id]
        if not runs:
            return None
        return max(runs, key=lambda record: record.updated_at or record.started_at)

    def active_for_session(self, portal_session_id: str) -> ChatRunRecord | None:
        runs = [record for record in self._runs.values() if record.portal_session_id == portal_session_id and record.status in ACTIVE_RUN_STATUSES]
        if not runs:
            return None
        return max(runs, key=lambda record: record.updated_at or record.started_at)

    def list_active(self) -> list[ChatRunRecord]:
        runs = [record for record in self._runs.values() if record.status in ACTIVE_RUN_STATUSES]
        runs.sort(key=lambda record: record.updated_at or record.started_at, reverse=True)
        return runs

    def list_for_session(self, portal_session_id: str, limit: int = 20) -> list[ChatRunRecord]:
        runs = [record for record in self._runs.values() if record.portal_session_id == portal_session_id]
        runs.sort(key=lambda record: record.updated_at or record.started_at, reverse=True)
        return runs[: max(0, int(limit))]

    def diagnostics_for(self, record: ChatRunRecord | None) -> dict[str, Any]:
        if record is None:
            return {}
        metadata = _safe_dict(record.metadata, 4000)
        last_transport_error = metadata.get("last_transport_error")
        if not isinstance(last_transport_error, dict):
            last_transport_error = {}
        diagnostics = {
            "last_transport_error": {
                key: last_transport_error.get(key)
                for key in ("exception_type", "method", "path", "recoverable")
                if last_transport_error.get(key) is not None
            },
            "opencode_process_status": metadata.get("opencode_process_status"),
            "restart_attempted": bool(metadata.get("restart_attempted", False)),
            "restart_status": metadata.get("restart_status"),
            "recovery_state": metadata.get("recovery_state"),
            "opencode_disconnected": bool(metadata.get("opencode_disconnected", False)),
            "opencode_may_still_be_running": bool(metadata.get("opencode_may_still_be_running", False)),
        }
        return _safe_dict({key: value for key, value in diagnostics.items() if value not in ({}, None, "")}, 4000)

    def to_public_dict(self, record: ChatRunRecord | None, *, include_final_payload: bool = True) -> dict[str, Any] | None:
        if record is None:
            return None
        payload: dict[str, Any] = {
            "request_id": record.request_id,
            "session_id": record.portal_session_id,
            "opencode_session_id": record.opencode_session_id,
            "status": record.status,
            "stream_state": record.stream_state,
            "completion_state": record.completion_state,
            "incomplete_reason": record.incomplete_reason,
            "assistant_message_id": record.assistant_message_id,
            "assistant_message_ids": list(record.assistant_message_ids),
            "last_response_text": record.last_response_text,
            "last_display_blocks": list(record.last_display_blocks),
            "started_at": record.started_at,
            "updated_at": record.updated_at,
            "completed_at": record.completed_at,
            "last_event_at": record.last_event_at,
            "metadata": _safe_dict(record.metadata, 4000),
            "diagnostics": self.diagnostics_for(record),
        }
        if include_final_payload and record.status in {"completed", "incomplete", "failed"}:
            payload["final_payload"] = _safe_dict(record.final_payload, 12000)
        return payload

    def to_session_summary(self, record: ChatRunRecord | None) -> dict[str, Any] | None:
        if record is None:
            return None
        return {
            "request_id": record.request_id,
            "status": record.status,
            "stream_state": record.stream_state,
            "last_event_at": record.last_event_at,
            "assistant_message_id": record.assistant_message_id,
            "assistant_message_ids": list(record.assistant_message_ids),
            "last_response_text": record.last_response_text,
            "updated_at": record.updated_at,
            "completed_at": record.completed_at,
        }
