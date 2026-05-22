from __future__ import annotations

from typing import Any

from efp_opencode_adapter.opencode_client import OpenCodeClientError


class FakeOpenCodeClient:
    def __init__(self, fork_mode: str = "include_boundary"):
        self.sessions: dict[str, dict[str, Any]] = {}
        self.messages: dict[str, list[dict[str, Any]]] = {}
        self.next_id = 1
        self.create_calls = 0
        self.abort_session_called = 0
        self.abort_tree_calls: list[str] = []
        self.fork_mode = fork_mode
        self.fork_calls: list[dict[str, Any]] = []
        self.revert_calls: list[dict[str, Any]] = []
        self.permission_calls: list[dict[str, Any]] = []

    async def health(self):
        return {"healthy": True, "version": "1.14.39"}

    async def create_session(self, title=None, parent_id=None):
        self.create_calls += 1
        sid = f"ses-{self.next_id}"
        self.next_id += 1
        self.sessions[sid] = {"id": sid, "title": title or "Chat"}
        if parent_id:
            self.sessions[sid]["parentID"] = parent_id
        self.messages[sid] = []
        return {"id": sid, "title": title or "Chat"}

    async def list_sessions(self):
        return list(self.sessions.values())

    async def get_session(self, session_id):
        if session_id not in self.sessions:
            raise OpenCodeClientError("not found", status=404)
        return self.sessions[session_id]

    async def get_session_status(self):
        return {"sessions": {sid: {"state": "running"} for sid in self.sessions}}

    async def list_session_children(self, session_id):
        if session_id not in self.sessions:
            raise OpenCodeClientError("not found", status=404)
        return []

    async def children(self, session_id):
        return await self.list_session_children(session_id)

    async def patch_session(self, session_id, title):
        if session_id not in self.sessions:
            raise OpenCodeClientError("not found", status=404)
        self.sessions[session_id]["title"] = title
        return self.sessions[session_id]

    async def delete_session(self, session_id):
        self.sessions.pop(session_id, None)
        self.messages.pop(session_id, None)

    async def list_messages(self, session_id, limit=None):
        return list(self.messages.get(session_id, []))

    async def get_message(self, session_id, message_id):
        for message in self.messages.get(session_id, []):
            if message.get("id") == message_id:
                return message
        return {}

    async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
        user_text = parts[0].get("text", "")
        user = {"id": f"u-{len(self.messages[session_id])+1}", "role": "user", "parts": [{"type": "text", "text": user_text}]}
        assistant = {
            "id": f"a-{len(self.messages[session_id])+2}",
            "role": "assistant",
            "parts": [{"type": "text", "text": f"echo: {user_text}"}],
        }
        self.messages[session_id].extend([user, assistant])
        return {"message": assistant, "usage": {"input_tokens": 10, "output_tokens": 5, "cost": 0.001}, "model": model or "test-model", "provider": "test-provider"}

    async def fork_session(self, session_id, message_id=None):
        if session_id not in self.sessions:
            raise OpenCodeClientError("not found", status=404)
        self.fork_calls.append({"session_id": session_id, "message_id": message_id})
        sid = f"ses-{self.next_id}"
        self.next_id += 1
        self.sessions[sid] = {"id": sid, "title": self.sessions[session_id].get("title", "Chat")}
        old_messages = self.messages.get(session_id, [])
        if not message_id:
            self.messages[sid] = list(old_messages)
        else:
            index = next((i for i, msg in enumerate(old_messages) if msg.get("id") == message_id), -1)
            if index < 0:
                raise OpenCodeClientError("message not found", status=404)
            if self.fork_mode == "assistant_boundary_drops_assistant" and old_messages[index].get("role") == "assistant":
                self.messages[sid] = list(old_messages[:index])
            elif self.fork_mode == "assistant_boundary_drops_assistant" and old_messages[index].get("role") == "user" and index == 0:
                self.messages[sid] = list(old_messages[: min(len(old_messages), 2)])
            elif self.fork_mode == "all_forks_bad_prefix":
                self.messages[sid] = list(old_messages[:1])
            else:
                self.messages[sid] = list(old_messages[: index + 1])
        return {"id": sid, "title": self.sessions[sid]["title"]}

    async def fork(self, session_id, message_id=None):
        return await self.fork_session(session_id, message_id)

    async def abort_session(self, session_id):
        self.abort_session_called += 1
        return {"success": True, "supported": True, "status": 200}

    async def abort_session_tree(self, session_id):
        self.abort_tree_calls.append(session_id)
        await self.abort_session(session_id)
        return {"success": True, "supported": True, "aborted_session_ids": [session_id], "missing_session_ids": [], "errors": []}

    async def revert_message(self, session_id, message_id, part_id=None):
        self.revert_calls.append({"session_id": session_id, "message_id": message_id, "part_id": part_id})
        messages = self.messages.get(session_id, [])
        index = next((i for i, msg in enumerate(messages) if msg.get("id") == message_id), -1)
        if index < 0:
            raise OpenCodeClientError("message not found", status=404)
        self.messages[session_id] = list(messages[:index])
        return {"success": True, "supported": True, "status": 200}

    async def permission_response(self, session_id, permission_id, payload):
        self.permission_calls.append({"session_id": session_id, "permission_id": permission_id, "payload": payload})
        if session_id not in self.sessions:
            raise OpenCodeClientError("not found", status=404)
        return {"success": True}

    async def respond_permission(self, session_id, permission_id, payload):
        return await self.permission_response(session_id, permission_id, payload)

    async def todo(self, session_id):
        if session_id not in self.sessions:
            raise OpenCodeClientError("not found", status=404)
        return []

    async def diff(self, session_id, message_id=None):
        if session_id not in self.sessions:
            raise OpenCodeClientError("not found", status=404)
        return {"files": [], "message_id": message_id}

    async def mcp_status(self):
        return {"servers": {}}

    async def event_stream(self):
        if False:
            yield {}
