from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class RequestBinding:
    portal_session_id: str
    request_id: str
    opencode_session_id: str
    message_id: str = ""
    task_id: str = ""
    kind: str = "chat"
    created_at: float = 0.0
    expires_at: float = 0.0
    completed: bool = False


class RequestBindingStore:
    def __init__(self) -> None:
        self._active: dict[str, RequestBinding] = {}
        self._by_message: dict[tuple[str, str], RequestBinding] = {}
        self._by_task: dict[tuple[str, str], RequestBinding] = {}

    def _new_binding(self, *, opencode_session_id: str, portal_session_id: str, request_id: str, kind: str, message_id: str = "", task_id: str = "", ttl_seconds: int = 3600) -> RequestBinding:
        now = time.time()
        return RequestBinding(
            portal_session_id=portal_session_id,
            request_id=request_id,
            opencode_session_id=opencode_session_id,
            message_id=message_id,
            task_id=task_id,
            kind=kind,
            created_at=now,
            expires_at=now + max(1, int(ttl_seconds)),
            completed=False,
        )

    def bind_active(self, opencode_session_id: str, portal_session_id: str, request_id: str, kind: str = "chat", task_id: str = "", ttl_seconds: int = 3600) -> None:
        self.prune_expired()
        binding = self._new_binding(opencode_session_id=opencode_session_id, portal_session_id=portal_session_id, request_id=request_id, kind=kind, task_id=task_id, ttl_seconds=ttl_seconds)
        self._active[opencode_session_id] = binding
        if task_id:
            self._by_task[(opencode_session_id, task_id)] = binding

    def bind_message(self, opencode_session_id: str, message_id: str, portal_session_id: str, request_id: str, kind: str = "chat", task_id: str = "", ttl_seconds: int = 3600) -> None:
        if not message_id:
            return
        self.prune_expired()
        binding = self._new_binding(opencode_session_id=opencode_session_id, portal_session_id=portal_session_id, request_id=request_id, kind=kind, message_id=message_id, task_id=task_id, ttl_seconds=ttl_seconds)
        self._by_message[(opencode_session_id, message_id)] = binding
        self._active[opencode_session_id] = binding
        if task_id:
            self._by_task[(opencode_session_id, task_id)] = binding

    def resolve(self, opencode_session_id: str, message_id: str = "", task_id: str = "") -> RequestBinding | None:
        self.prune_expired()
        binding = None
        if message_id:
            binding = self._by_message.get((opencode_session_id, message_id))
        if binding is None and task_id:
            binding = self._by_task.get((opencode_session_id, task_id))
        if binding is None:
            binding = self._active.get(opencode_session_id)
        if binding and not binding.completed and binding.expires_at > time.time():
            return binding
        return None

    def complete(self, request_id: str) -> None:
        for binding in list(self._active.values()) + list(self._by_message.values()) + list(self._by_task.values()):
            if binding.request_id == request_id:
                binding.completed = True

    def prune_expired(self) -> None:
        now = time.time()
        self._active = {k: v for k, v in self._active.items() if (not v.completed and v.expires_at > now)}
        self._by_message = {k: v for k, v in self._by_message.items() if (not v.completed and v.expires_at > now)}
        self._by_task = {k: v for k, v in self._by_task.items() if (not v.completed and v.expires_at > now)}
