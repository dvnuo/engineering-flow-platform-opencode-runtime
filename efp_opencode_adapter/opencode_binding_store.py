from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


SCHEMA_VERSION = "opencode_conversation_binding.v1"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class OpenCodeConversationBinding:
    portal_conversation_id: str
    agent_id: str
    opencode_session_id: str
    title: str
    created_at: str
    updated_at: str
    archived_at: str | None = None
    source: str = "opencode"
    schema_version: str = SCHEMA_VERSION
    metadata: dict[str, Any] = field(default_factory=dict)


def binding_to_public(binding: OpenCodeConversationBinding) -> dict[str, Any]:
    return {
        "id": binding.portal_conversation_id,
        "portal_conversation_id": binding.portal_conversation_id,
        "agent_id": binding.agent_id,
        "opencode_session_id": binding.opencode_session_id,
        "title": binding.title,
        "source": binding.source,
        "created_at": binding.created_at,
        "updated_at": binding.updated_at,
        "archived_at": binding.archived_at,
        "schema_version": binding.schema_version,
    }


class OpenCodeBindingStore:
    """Persist Portal conversation id <-> OpenCode root session id bindings only."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._bindings: dict[str, OpenCodeConversationBinding] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self._bindings = {}
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            self._bindings = {}
            return
        raw_bindings = payload.get("bindings") if isinstance(payload, dict) else {}
        if not isinstance(raw_bindings, dict):
            self._bindings = {}
            return
        loaded: dict[str, OpenCodeConversationBinding] = {}
        for key, raw in raw_bindings.items():
            if not isinstance(raw, dict):
                continue
            opencode_session_id = str(raw.get("opencode_session_id") or "")
            if not opencode_session_id:
                continue
            conversation_id = str(raw.get("portal_conversation_id") or key)
            try:
                loaded[conversation_id] = OpenCodeConversationBinding(
                    portal_conversation_id=conversation_id,
                    agent_id=str(raw.get("agent_id") or ""),
                    opencode_session_id=opencode_session_id,
                    title=str(raw.get("title") or ""),
                    created_at=str(raw.get("created_at") or _utc_now_iso()),
                    updated_at=str(raw.get("updated_at") or _utc_now_iso()),
                    archived_at=str(raw.get("archived_at")) if raw.get("archived_at") else None,
                    source=str(raw.get("source") or "opencode"),
                    schema_version=str(raw.get("schema_version") or SCHEMA_VERSION),
                    metadata=raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {},
                )
            except Exception:
                continue
        self._bindings = loaded

    reload = load

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"bindings": {key: asdict(value) for key, value in self._bindings.items()}}
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

    def create(self, agent_id: str, opencode_session_id: str, title: str = "") -> OpenCodeConversationBinding:
        session_id = str(opencode_session_id or "").strip()
        if not session_id:
            raise ValueError("opencode_session_id_required")
        now = _utc_now_iso()
        conversation_id = f"pc_{uuid4().hex}"
        binding = OpenCodeConversationBinding(
            portal_conversation_id=conversation_id,
            agent_id=str(agent_id or ""),
            opencode_session_id=session_id,
            title=str(title or ""),
            created_at=now,
            updated_at=now,
        )
        self._bindings[conversation_id] = binding
        self.save()
        return binding

    def get(self, conversation_id: str) -> OpenCodeConversationBinding | None:
        return self._bindings.get(str(conversation_id or ""))

    def list(self, agent_id: str, include_archived: bool = False) -> list[OpenCodeConversationBinding]:
        target_agent = str(agent_id or "")
        out = []
        for binding in self._bindings.values():
            if target_agent and binding.agent_id != target_agent:
                continue
            if binding.archived_at and not include_archived:
                continue
            out.append(binding)
        return sorted(out, key=lambda item: item.updated_at, reverse=True)

    def update_title(self, conversation_id: str, title: str) -> OpenCodeConversationBinding:
        binding = self._require(conversation_id)
        updated = OpenCodeConversationBinding(
            **{
                **asdict(binding),
                "title": str(title or ""),
                "updated_at": _utc_now_iso(),
            }
        )
        self._bindings[updated.portal_conversation_id] = updated
        self.save()
        return updated

    def archive(self, conversation_id: str) -> OpenCodeConversationBinding:
        binding = self._require(conversation_id)
        now = _utc_now_iso()
        updated = OpenCodeConversationBinding(
            **{
                **asdict(binding),
                "archived_at": binding.archived_at or now,
                "updated_at": now,
            }
        )
        self._bindings[updated.portal_conversation_id] = updated
        self.save()
        return updated

    def replace_opencode_session(self, conversation_id: str, new_session_id: str) -> OpenCodeConversationBinding:
        binding = self._require(conversation_id)
        session_id = str(new_session_id or "").strip()
        if not session_id:
            raise ValueError("opencode_session_id_required")
        updated = OpenCodeConversationBinding(
            **{
                **asdict(binding),
                "opencode_session_id": session_id,
                "updated_at": _utc_now_iso(),
            }
        )
        self._bindings[updated.portal_conversation_id] = updated
        self.save()
        return updated

    def _require(self, conversation_id: str) -> OpenCodeConversationBinding:
        binding = self.get(conversation_id)
        if binding is None:
            raise KeyError("conversation_not_found")
        return binding
