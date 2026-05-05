import os
import uuid

import pytest


def test_chat_contract_when_enabled(post_json):
    if os.getenv("RUNTIME_CONTRACT_ENABLE_CHAT") != "1":
        pytest.skip("chat contract disabled")
    status, body = post_json(
        "/api/chat",
        {
            "message": "hello",
            "session_id": "contract-chat",
            "request_id": "contract-chat-request",
        },
    )
    assert status == 200
    assert "engine" in body or "response" in body or "error" in body


def test_task_contract_when_enabled(post_json, get_json):
    if os.getenv("RUNTIME_CONTRACT_ENABLE_TASKS") != "1":
        pytest.skip("task contract disabled")

    task_id = f"contract-task-{uuid.uuid4().hex[:8]}"
    status, body = post_json(
        "/api/tasks/execute",
        {
            "task_id": task_id,
            "task_type": "generic_agent_task",
            "input_payload": {"goal": "ping"},
            "metadata": {"contract": True},
            "session_id": "contract-task-session",
            "request_id": f"contract-request-{task_id}",
        },
    )

    assert status in {200, 202}
    assert body["task_id"] == task_id

    _, detail = get_json(f"/api/tasks/{task_id}")
    assert detail["task_id"] == task_id
    assert detail["status"] in {
        "accepted",
        "running",
        "success",
        "error",
        "blocked",
        "cancelled",
    }


    if detail["status"] in {"accepted", "running"}:
        cancel_status, cancel_body = post_json(f"/api/tasks/{task_id}/cancel", {})
        assert cancel_status == 200
        assert cancel_body["task_id"] == task_id
        assert cancel_body["status"] in {"cancelled", "success", "error", "blocked"}
