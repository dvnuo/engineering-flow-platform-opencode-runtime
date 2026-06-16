from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from .thinking_events import utc_now_iso


TERMINAL_STATES = {"completed", "failed", "cancelled"}


@dataclass
class ChatRunRecord:
    request_id: str
    session_id: str
    engine: str = "opencode"
    state: str = "running"
    started_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    latest_event_at: str = ""
    latest_event_seq: int = 0
    replay_available: bool = True
    detached_viewers: int = 0
    final_payload: dict[str, Any] | None = None
    error_payload: dict[str, Any] | None = None
    task: asyncio.Task | None = field(default=None, repr=False)

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def to_payload(self) -> dict[str, Any]:
        return {
            "ok": True,
            "engine": self.engine,
            "session_id": self.session_id,
            "request_id": self.request_id,
            "state": self.state,
            "terminal": self.terminal,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "latest_event_at": self.latest_event_at,
            "latest_event_seq": self.latest_event_seq,
            "replay_available": self.replay_available,
            "detached_viewers": self.detached_viewers,
            "final_payload": self.final_payload,
            "error_payload": self.error_payload,
        }


class ChatRunRegistry:
    def __init__(self, *, max_records: int = 512) -> None:
        self._records: dict[str, ChatRunRecord] = {}
        self._max_records = max_records

    def start(self, *, session_id: str, request_id: str) -> ChatRunRecord:
        existing = self.get(request_id, session_id=session_id)
        if existing is not None:
            return existing
        record = ChatRunRecord(request_id=request_id, session_id=session_id)
        self._records[request_id] = record
        self._prune_terminal_records()
        return record

    def _prune_terminal_records(self) -> None:
        if len(self._records) <= self._max_records:
            return
        overflow = len(self._records) - self._max_records
        terminal_records = sorted(
            (record for record in self._records.values() if record.terminal),
            key=lambda record: record.updated_at,
        )
        for record in terminal_records[:overflow]:
            self._records.pop(record.request_id, None)

    def get(self, request_id: str, *, session_id: str | None = None) -> ChatRunRecord | None:
        rid = str(request_id or "").strip()
        if not rid:
            return None
        record = self._records.get(rid)
        if record is None:
            return None
        if session_id and record.session_id != session_id:
            return None
        return record

    def attach_task(self, request_id: str, task: asyncio.Task) -> None:
        record = self.get(request_id)
        if record is not None:
            record.task = task
            record.updated_at = utc_now_iso()

    def record_event(self, request_id: str, event: dict[str, Any]) -> None:
        record = self.get(request_id)
        if record is None or record.terminal:
            return
        record.latest_event_seq += 1
        record.latest_event_at = str(event.get("created_at") or utc_now_iso())
        record.updated_at = utc_now_iso()

    def mark_detached(self, request_id: str) -> None:
        record = self.get(request_id)
        if record is None or record.terminal:
            return
        record.detached_viewers += 1
        record.updated_at = utc_now_iso()

    def complete(self, request_id: str, final_payload: dict[str, Any]) -> None:
        record = self.get(request_id)
        if record is None:
            return
        state = "completed" if final_payload.get("ok") is not False and final_payload.get("completion_state") in {"", None, "completed", "success"} else str(final_payload.get("completion_state") or "failed")
        record.state = "completed" if state in {"completed", "success"} else "failed"
        record.final_payload = dict(final_payload or {})
        record.updated_at = utc_now_iso()

    def fail(self, request_id: str, error_payload: dict[str, Any]) -> None:
        record = self.get(request_id)
        if record is None:
            return
        if record.state == "cancelled":
            return
        record.state = "failed"
        record.error_payload = dict(error_payload or {})
        record.updated_at = utc_now_iso()

    def cancel(self, request_id: str) -> bool:
        record = self.get(request_id)
        if record is None or record.terminal:
            return False
        record.state = "cancelled"
        record.updated_at = utc_now_iso()
        if record.task is not None and not record.task.done():
            record.task.cancel()
        return True


chat_run_registry = ChatRunRegistry()
