from __future__ import annotations

from typing import Any, Mapping


SUPPORTED_REASONING_EFFORTS = ("low", "medium", "high", "xhigh")


def normalize_reasoning_effort(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("reasoning_effort_must_be_string")
    normalized = value.strip().lower()
    if not normalized:
        return None
    if normalized not in SUPPORTED_REASONING_EFFORTS:
        raise ValueError("unsupported_reasoning_effort")
    return normalized


def reasoning_effort_from_metadata(metadata: Mapping[str, Any] | None) -> str | None:
    source = metadata if isinstance(metadata, Mapping) else {}
    inference = source.get("inference") if isinstance(source.get("inference"), Mapping) else {}
    runtime_profile = source.get("runtime_profile") if isinstance(source.get("runtime_profile"), Mapping) else {}
    config = runtime_profile.get("config") if isinstance(runtime_profile.get("config"), Mapping) else {}
    llm = config.get("llm") if isinstance(config.get("llm"), Mapping) else {}
    for candidate in (
        inference.get("reasoning_effort"),
        source.get("reasoning_effort"),
        llm.get("reasoning_effort"),
    ):
        normalized = normalize_reasoning_effort(candidate)
        if normalized:
            return normalized
    return None
