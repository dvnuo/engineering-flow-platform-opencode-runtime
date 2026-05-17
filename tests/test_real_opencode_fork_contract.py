from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import pytest

from efp_opencode_adapter.opencode_client import OpenCodeClient
from efp_opencode_adapter.opencode_message_adapter import message_to_visible_text
from efp_opencode_adapter.settings import Settings


def _extract_session_id(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("id", "session_id", "sessionID", "uuid"):
        value = payload.get(key)
        if value:
            return str(value)
    for key in ("session", "data", "info"):
        nested_id = _extract_session_id(payload.get(key))
        if nested_id:
            return nested_id
    return ""


def _message_info(message: Any) -> dict[str, Any]:
    if isinstance(message, dict) and isinstance(message.get("info"), dict):
        return message["info"]
    return message if isinstance(message, dict) else {}


def _message_id(message: Any) -> str:
    info = _message_info(message)
    if info.get("id"):
        return str(info["id"])
    if isinstance(message, dict):
        for key in ("id", "message_id"):
            if message.get(key):
                return str(message[key])
    return ""


def _message_role(message: Any) -> str:
    info = _message_info(message)
    role = info.get("role") or (message.get("role") if isinstance(message, dict) else "")
    return str(role or "").lower()


def _normalized_text(message: Any) -> str:
    return message_to_visible_text(message).replace("\r\n", "\n").replace("\r", "\n").strip()


def _visible_signatures(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {"role": _message_role(message), "content": _normalized_text(message)}
        for message in messages
        if _message_role(message) in {"user", "assistant"}
    ]


def _compact_signatures(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    out = []
    for sig in _visible_signatures(messages):
        content = sig["content"]
        if len(content) > 200:
            content = content[:200] + "..."
        out.append({"role": sig["role"], "content": content})
    return out


async def _wait_for_assistant_turn(client: OpenCodeClient, session_id: str, *, min_visible_count: int) -> list[dict[str, Any]]:
    deadline = asyncio.get_running_loop().time() + 60
    last_messages: list[dict[str, Any]] = []
    while asyncio.get_running_loop().time() < deadline:
        last_messages = await client.list_messages(session_id)
        visible = [message for message in last_messages if _message_role(message) in {"user", "assistant"}]
        if (
            len(visible) >= min_visible_count
            and _message_role(visible[-1]) == "assistant"
            and _normalized_text(visible[-1])
        ):
            return last_messages
        await asyncio.sleep(1)
    roles = [_message_role(message) for message in last_messages]
    pytest.fail(f"timed out waiting for assistant completion; observed roles={roles}")


@pytest.mark.asyncio
async def test_real_opencode_fork_candidates_preserve_expected_prefix():
    opencode_url = os.getenv("EFP_REAL_OPENCODE_URL")
    if not opencode_url:
        pytest.skip("EFP_REAL_OPENCODE_URL is not set")

    client = OpenCodeClient(Settings.from_env(opencode_url=opencode_url))
    created_session_ids: list[str] = []
    try:
        created = await client.create_session(title="efp fork contract")
        old_session_id = _extract_session_id(created)
        assert old_session_id
        created_session_ids.append(old_session_id)

        await client.send_message(old_session_id, parts=[{"type": "text", "text": "hi"}], model=None, agent=None)
        await _wait_for_assistant_turn(client, old_session_id, min_visible_count=2)

        await client.send_message(old_session_id, parts=[{"type": "text", "text": "how are you"}], model=None, agent=None)
        old_messages = await _wait_for_assistant_turn(client, old_session_id, min_visible_count=4)

        target_idx = next(
            (
                idx
                for idx, message in enumerate(old_messages)
                if _message_role(message) == "user" and _normalized_text(message) == "how are you"
            ),
            -1,
        )
        assert target_idx > 0
        expected_prefix = _visible_signatures(old_messages[:target_idx])
        assert [item["role"] for item in expected_prefix] == ["user", "assistant"]

        candidates: list[dict[str, str]] = []
        seen: set[str] = set()

        def add_candidate(message: dict[str, Any] | None) -> None:
            if not message:
                return
            message_id = _message_id(message)
            if not message_id or message_id in seen:
                return
            seen.add(message_id)
            candidates.append({"message_id": message_id, "role": _message_role(message)})

        add_candidate(old_messages[target_idx])
        add_candidate(old_messages[target_idx - 1] if target_idx > 0 else None)
        for idx in range(target_idx - 1, -1, -1):
            if _message_role(old_messages[idx]) == "user":
                add_candidate(old_messages[idx])
                break

        attempts: list[dict[str, Any]] = []
        matched = False
        for candidate in candidates:
            forked = await client.fork_session(old_session_id, candidate["message_id"])
            forked_session_id = _extract_session_id(forked)
            assert forked_session_id
            created_session_ids.append(forked_session_id)

            forked_messages = await client.list_messages(forked_session_id)
            actual_prefix = _visible_signatures(forked_messages)
            candidate_matched = actual_prefix == expected_prefix
            matched = matched or candidate_matched
            attempts.append(
                {
                    "role": candidate["role"],
                    "message_id": candidate["message_id"],
                    "matched": candidate_matched,
                    "actual_signatures": _compact_signatures(forked_messages),
                }
            )

        if not matched:
            compact_expected = [
                {
                    "role": item["role"],
                    "content": item["content"][:200] + ("..." if len(item["content"]) > 200 else ""),
                }
                for item in expected_prefix
            ]
            pytest.fail(
                "no fork boundary preserved expected prefix: "
                + json.dumps(
                    {
                        "expected_signatures": compact_expected,
                        "attempts": attempts,
                    },
                    ensure_ascii=False,
                )
            )
    finally:
        for session_id in reversed(created_session_ids):
            try:
                await client.delete_session(session_id)
            except Exception:
                pass
