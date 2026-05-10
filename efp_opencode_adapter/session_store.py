from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _safe_int(value, default: int = 0) -> int:
    if value is None or isinstance(value, bool):
        return default
    try:
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return default
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


@dataclass
class SessionRecord:
    portal_session_id: str
    opencode_session_id: str
    title: str
    agent: str | None
    model: str | None
    created_at: str
    updated_at: str
    last_message: str
    message_count: int
    deleted: bool = False
    partial_recovery: bool = False


class SessionDeletedError(RuntimeError):
    pass


class SessionStore:
    def __init__(self, sessions_dir: Path):
        self.sessions_dir = sessions_dir
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.sessions_dir / "index.json"
        self._sessions: dict[str, SessionRecord] = {}
        self.load()

    def _quarantine_corrupt_index(self) -> Path | None:
        if not self.index_path.exists():
            return None

        stamp = _utc_now_iso().replace(":", "").replace("+", "_").replace("/", "_")
        backup = self.index_path.with_name(f"{self.index_path.name}.corrupt-{stamp}")

        try:
            self.index_path.replace(backup)
            return backup
        except Exception:
            return None

    def load(self) -> None:
        if not self.index_path.exists():
            self._sessions = {}
            return

        try:
            payload = json.loads(self.index_path.read_text(encoding="utf-8"))
        except Exception:
            self._quarantine_corrupt_index()
            self._sessions = {}
            return

        if not isinstance(payload, dict):
            self._quarantine_corrupt_index()
            self._sessions = {}
            return

        sessions = payload.get("sessions", {})
        if not isinstance(sessions, dict):
            self._sessions = {}
            return

        loaded: dict[str, SessionRecord] = {}

        for portal_id, raw in sessions.items():
            if not isinstance(raw, dict):
                continue

            try:
                kwargs = {
                    "portal_session_id": str(raw.get("portal_session_id", portal_id)),
                    "opencode_session_id": str(raw.get("opencode_session_id", "") or ""),
                    "title": str(raw.get("title", "Chat") or "Chat"),
                    "agent": raw.get("agent") if isinstance(raw.get("agent"), str) else None,
                    "model": raw.get("model") if isinstance(raw.get("model"), str) else None,
                    "created_at": raw.get("created_at") if isinstance(raw.get("created_at"), str) else _utc_now_iso(),
                    "updated_at": raw.get("updated_at") if isinstance(raw.get("updated_at"), str) else _utc_now_iso(),
                    "last_message": raw.get("last_message") if isinstance(raw.get("last_message"), str) else "",
                    "message_count": _safe_int(raw.get("message_count")),
                    "deleted": bool(raw.get("deleted", False)),
                    "partial_recovery": bool(raw.get("partial_recovery", False)),
                }
            except Exception:
                continue

            if kwargs["opencode_session_id"]:
                loaded[str(portal_id)] = SessionRecord(**kwargs)

        self._sessions = loaded

    reload = load

    def save(self) -> None:
        payload = {"sessions": {k: asdict(v) for k, v in self._sessions.items()}}
        tmp_path = self.index_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(self.index_path)

    def list_active(self) -> list[SessionRecord]:
        return [s for s in self._sessions.values() if not s.deleted]

    def get(self, portal_session_id: str) -> SessionRecord | None:
        return self._sessions.get(portal_session_id)

    def find_by_opencode_session_id(self, opencode_session_id: str) -> SessionRecord | None:
        for rec in self.list_active():
            if rec.opencode_session_id == opencode_session_id:
                return rec
        return None

    def upsert(self, record: SessionRecord) -> SessionRecord:
        self._sessions[record.portal_session_id] = record
        self.save()
        return record

    def rename(self, portal_session_id: str, title: str) -> SessionRecord:
        record = self._sessions[portal_session_id]
        updated = SessionRecord(**{**asdict(record), "title": title, "updated_at": _utc_now_iso()})
        return self.upsert(updated)

    def mark_deleted(self, portal_session_id: str) -> SessionRecord | None:
        record = self._sessions.get(portal_session_id)
        if not record:
            return None
        updated = SessionRecord(**{**asdict(record), "deleted": True, "updated_at": _utc_now_iso()})
        return self.upsert(updated)

    def clear(self) -> list[SessionRecord]:
        cleared: list[SessionRecord] = []
        for record in self.list_active():
            cleared_record = SessionRecord(**{**asdict(record), "deleted": True, "updated_at": _utc_now_iso()})
            self._sessions[record.portal_session_id] = cleared_record
            cleared.append(cleared_record)
        self.save()
        return cleared

    def update_after_chat(
        self,
        portal_session_id: str,
        last_message: str,
        assistant_text: str,
        model: str | None,
        agent: str | None,
    ) -> SessionRecord:
        record = self._sessions[portal_session_id]
        if record.deleted:
            return record
        updated = SessionRecord(
            **{
                **asdict(record),
                "model": model or record.model,
                "agent": agent or record.agent,
                "last_message": assistant_text or last_message,
                "message_count": max(record.message_count + 2, 2),
                "updated_at": _utc_now_iso(),
                "deleted": False,
            }
        )
        return self.upsert(updated)

    def replace_opencode_session_after_mutation(
        self,
        portal_session_id: str,
        opencode_session_id: str,
        *,
        message_count: int,
        last_message: str = "",
    ) -> SessionRecord:
        record = self._sessions[portal_session_id]
        if record.deleted:
            raise SessionDeletedError("session_deleted")
        updated = SessionRecord(
            portal_session_id=record.portal_session_id,
            opencode_session_id=opencode_session_id,
            title=record.title,
            agent=record.agent,
            model=record.model,
            created_at=record.created_at,
            updated_at=_utc_now_iso(),
            last_message=last_message,
            message_count=max(0, int(message_count)),
            deleted=False,
            partial_recovery=record.partial_recovery,
        )
        return self.upsert(updated)
