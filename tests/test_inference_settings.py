import pytest

from efp_opencode_adapter.inference_settings import (
    normalize_reasoning_effort,
    reasoning_effort_from_metadata,
)


def test_reasoning_effort_prefers_explicit_inference_metadata():
    metadata = {
        "reasoning_effort": "medium",
        "inference": {"reasoning_effort": "xhigh"},
        "runtime_profile": {"config": {"llm": {"reasoning_effort": "low"}}},
    }

    assert reasoning_effort_from_metadata(metadata) == "xhigh"


def test_reasoning_effort_falls_back_to_runtime_profile():
    metadata = {"runtime_profile": {"config": {"llm": {"reasoning_effort": "HIGH"}}}}

    assert reasoning_effort_from_metadata(metadata) == "high"


def test_reasoning_effort_rejects_unsupported_values():
    with pytest.raises(ValueError, match="unsupported_reasoning_effort"):
        normalize_reasoning_effort("extreme")
