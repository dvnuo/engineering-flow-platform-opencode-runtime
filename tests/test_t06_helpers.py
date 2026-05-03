from __future__ import annotations

from typing import Any

from efp_opencode_adapter.opencode_client import OpenCodeClientError


class FakeOpenCodeClient:
    def __init__(self):
        self.sessions: dict[str, dict[str, Any]] = {}
        self.messages: dict[str, list[dict[str, Any]]] = {}
        self.next_id = 1
        self.create_calls = 0

    async def health(self):
        return {"healthy": True, "version": "1.14.29"}

    async def create_session(self, title=None):
        self.create_calls += 1
        sid = f"ses-{self.next_id}"
        self.next_id += 1
        self.sessions[sid] = {"id": sid, "title": title or "Chat"}
        self.messages[sid] = []
        return {"id": sid, "title": title or "Chat"}

    async def list_sessions(self):
        return list(self.sessions.values())

    async def get_session(self, session_id):
        if session_id not in self.sessions:
            raise OpenCodeClientError("not found", status=404)
        return self.sessions[session_id]

    async def patch_session(self, session_id, title):
        if session_id not in self.sessions:
            raise OpenCodeClientError("not found", status=404)
        self.sessions[session_id]["title"] = title
        return self.sessions[session_id]

    async def delete_session(self, session_id):
        self.sessions.pop(session_id, None)
        self.messages.pop(session_id, None)

    async def list_messages(self, session_id):
        return list(self.messages.get(session_id, []))

    async def send_message(self, session_id, *, parts, model, agent, system=None):
        user_text = parts[0].get("text", "")
        user = {"id": f"u-{len(self.messages[session_id])+1}", "role": "user", "parts": [{"type": "text", "text": user_text}]}
        assistant = {
            "id": f"a-{len(self.messages[session_id])+2}",
            "role": "assistant",
            "parts": [{"type": "text", "text": f"echo: {user_text}"}],
        }
        self.messages[session_id].extend([user, assistant])
        return {"message": assistant, "usage": {"input_tokens": 10, "output_tokens": 5, "cost": 0.001}, "model": model or "test-model", "provider": "test-provider"}

    async def respond_permission(self, session_id, permission_id, payload):
        if session_id not in self.sessions:
            raise OpenCodeClientError("not found", status=404)
        return {"success": True}
