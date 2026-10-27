"""Usage rows carry OpenCode's per-step tokens and the Copilot credits estimate the Portal shows."""

import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings
from efp_opencode_adapter.usage_tracker import UsageTracker
from test_t06_helpers import FakeOpenCodeClient


def _step(message_id: str, *, input_tokens: int, read: int, write: int, output: int, reasoning: int, model: str = "gpt-6.1-sol") -> dict:
    return {
        "info": {
            "id": message_id,
            "role": "assistant",
            "modelID": model,
            "providerID": "github-copilot",
            "finish": "stop",
            "tokens": {"input": input_tokens, "output": output, "reasoning": reasoning, "cache": {"read": read, "write": write}},
            "cost": 0,
        },
        "parts": [{"type": "text", "text": f"step {message_id}"}],
    }


# Two model steps: 4,000 uncached, 20,000 cached and 1,000 cache-write input
# tokens, 700 output and 300 reasoning tokens on GPT-6.1 Sol = $0.0225.
RUN = [
    _step("a1", input_tokens=1000, read=9000, write=1000, output=400, reasoning=300),
    _step("a2", input_tokens=3000, read=11000, write=0, output=300, reasoning=0),
]


def test_record_chat_sums_the_runs_steps_and_estimates_credits(tmp_path):
    tracker = UsageTracker(tmp_path / "usage.jsonl")

    rec = tracker.record_chat(
        session_id="s",
        request_id="r",
        model=None,
        provider=None,
        response_payload=RUN[-1],
        input_text="hello",
        output_text="done",
        run_messages=RUN,
    )

    assert rec["model"] == "gpt-6.1-sol"
    assert rec["provider"] == "github-copilot"
    # input_tokens is the whole prompt, cached and written tokens included.
    assert rec["input_tokens"] == 25_000
    assert rec["cached_input_tokens"] == 20_000
    assert rec["cache_write_tokens"] == 1_000
    assert rec["output_tokens"] == 700
    assert rec["reasoning_tokens"] == 300
    assert rec["iterations"] == 2
    assert rec["cache_hit_ratio"] == 0.8
    assert rec["estimated_credits"] == 2.25
    assert rec["pricing_tier"] == "default"
    assert rec["pricing"] == {"model": "gpt-6.1-sol", "tier": "default", "source": "static", "threshold_tokens": 272_000}
    # OpenCode said 0; the estimate fills the USD column the Usage card shows.
    assert rec["cost"] == 0.0225

    stored = [json.loads(line) for line in (tmp_path / "usage.jsonl").read_text(encoding="utf-8").splitlines()]
    assert stored[0]["estimated_credits"] == 2.25


def test_tier_follows_the_largest_single_request_of_the_run(tmp_path):
    tracker = UsageTracker(tmp_path / "usage.jsonl")
    run = [
        _step("a1", input_tokens=200_000, read=100_000, write=0, output=0, reasoning=0),
        _step("a2", input_tokens=1_000, read=0, write=0, output=0, reasoning=0),
    ]
    rec = tracker.record_chat(session_id="s", request_id="r", model="github-copilot/gpt-6.1-sol", provider="github-copilot", response_payload=run[-1], input_text="", output_text="", run_messages=run, iterations=7)
    assert rec["iterations"] == 7
    assert rec["pricing_tier"] == "long_context"
    # 201,000 uncached at $4 + 100,000 cached at $0.20 = $0.824.
    assert rec["estimated_credits"] == 82.4
    assert rec["cost"] == 0.824


def test_models_outside_the_table_keep_opencodes_cost_and_no_estimate(tmp_path):
    tracker = UsageTracker(tmp_path / "usage.jsonl")
    step = _step("a1", input_tokens=500, read=0, write=0, output=20, reasoning=0, model="claude-sonnet-5.5")
    step["info"]["cost"] = 0.0123
    rec = tracker.record_chat(session_id="s", request_id="r", model=None, provider="anthropic", response_payload=step, input_text="", output_text="", run_messages=[step])
    assert rec["model"] == "claude-sonnet-5.5"
    assert rec["estimated_credits"] is None
    assert rec["pricing_tier"] is None
    assert rec["cost"] == 0.0123
    assert rec["input_tokens"] == 500


def test_a_reported_cost_wins_over_the_estimate_for_the_usd_column(tmp_path):
    tracker = UsageTracker(tmp_path / "usage.jsonl")
    step = _step("a1", input_tokens=100_000, read=0, write=0, output=0, reasoning=0)
    step["info"]["cost"] = 1.5
    rec = tracker.record_chat(session_id="s", request_id="r", model=None, provider=None, response_payload=step, input_text="", output_text="", run_messages=[step])
    assert rec["cost"] == 1.5
    assert rec["estimated_credits"] == 20.0


def test_payload_usage_blocks_still_work_without_run_messages(tmp_path):
    tracker = UsageTracker(tmp_path / "usage.jsonl")
    rec = tracker.record_chat(
        session_id="s",
        request_id="r",
        model=None,
        provider=None,
        response_payload={"message": {"role": "assistant"}, "usage": {"input_tokens": 10, "output_tokens": 5, "cost": 0.001}, "model": "test-model", "provider": "test-provider"},
        input_text="hello",
        output_text="echo",
    )
    assert rec["input_tokens"] == 10
    assert rec["output_tokens"] == 5
    assert rec["cached_input_tokens"] == 0
    assert rec["cost"] == 0.001
    assert rec["iterations"] is None
    assert rec["estimated_credits"] is None

    # An OpenCode-shaped payload without a usage block books its info.tokens.
    rec = tracker.record_chat(session_id="s", request_id="r2", model=None, provider=None, response_payload=RUN[0], input_text="", output_text="")
    assert rec["input_tokens"] == 11_000
    assert rec["cached_input_tokens"] == 9_000
    assert rec["model"] == "gpt-6.1-sol"


def test_summary_adds_the_new_totals_and_keeps_old_rows(tmp_path):
    usage_file = tmp_path / "usage.jsonl"
    tracker = UsageTracker(usage_file)
    tracker.record_chat(session_id="s", request_id="r", model=None, provider=None, response_payload=RUN[-1], input_text="", output_text="", run_messages=RUN)
    # A row from before the credits estimate existed.
    old_row = {"timestamp": json.loads(usage_file.read_text(encoding="utf-8").splitlines()[0])["timestamp"], "type": "chat", "session_id": "old", "request_id": "old", "model": "gpt-6.1-sol", "provider": "github-copilot", "requests": 1, "messages": 2, "input_tokens": 5, "output_tokens": 1, "cost": 0.0}
    with usage_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(old_row) + "\n")

    summary = tracker.summarize(days=30)
    assert summary["global"]["total_requests"] == 2
    assert summary["global"]["total_input_tokens"] == 25_005
    assert summary["global"]["total_cached_input_tokens"] == 20_000
    assert summary["global"]["total_cache_write_tokens"] == 1_000
    assert summary["global"]["total_reasoning_tokens"] == 300
    assert summary["global"]["total_estimated_credits"] == 2.25
    assert summary["global"]["estimated_rows"] == 1
    by_model = {row["model"]: row for row in summary["by_model"]}
    assert by_model["gpt-6.1-sol"]["total_estimated_credits"] == 2.25
    assert by_model["gpt-6.1-sol"]["total_requests"] == 2


class StepwiseOpenCodeClient(FakeOpenCodeClient):
    """OpenCode's real shape: one assistant message per model step, tokens on each, the send answers with the last."""

    async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
        user_text = parts[0].get("text", "")
        count = len(self.messages[session_id])
        user = {"info": {"id": f"u-{count + 1}", "role": "user"}, "parts": [{"type": "text", "text": user_text}]}
        first = _step(f"a-{count + 2}", input_tokens=1000, read=9000, write=1000, output=400, reasoning=300)
        first["parts"] = [{"type": "tool", "tool": "read", "state": {"status": "completed", "output": "contents"}}]
        last = _step(f"a-{count + 3}", input_tokens=3000, read=11000, write=0, output=300, reasoning=0)
        last["parts"] = [{"type": "text", "text": f"answer: {user_text}"}]
        self.messages[session_id].extend([user, first, last])
        return last


@pytest.mark.asyncio
async def test_chat_answer_carries_the_runs_credits_for_the_portal(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=StepwiseOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post("/api/chat", json={"message": "hello", "session_id": "s-credits"})
        assert response.status == 200
        body = await response.json()
        assert body["response"] == "answer: hello"
        usage = body["usage"]
        assert usage["model"] == "gpt-6.1-sol"
        assert usage["iterations"] == 2
        assert usage["input_tokens"] == 25_000
        assert usage["cached_input_tokens"] == 20_000
        assert usage["reasoning_tokens"] == 300
        assert usage["cache_hit_ratio"] == 0.8
        assert usage["estimated_credits"] == 2.25
        assert usage["pricing"]["tier"] == "default"
        assert usage["pricing"]["threshold_tokens"] == 272_000

        totals = await (await client.get("/api/usage?days=30")).json()
        assert totals["global"]["total_estimated_credits"] == 2.25
        assert totals["global"]["total_cached_input_tokens"] == 20_000
    finally:
        await client.close()
