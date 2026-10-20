"""Copilot "auto" constants and pure helpers shared by the config generator and the proxy.

Kept free of package imports so ``opencode_config`` (which the Copilot auth
module's import chain reaches) and ``copilot_auto_session`` can both use it.
Mirrors the native runtime's ``efp_runtime/llm/models.py`` (AUTO_MODEL_ID,
COPILOT_AUTO_TIERS, auto_tier_for, copilot_model_context_profile) and the
admission rules of ``GitHubCopilotProvider.resolve_auto_model``; keep them
aligned.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

AUTO_MODEL_ID = "auto"
# Copilot "auto" is VS Code's routing mode: the turn's prompt and a tier go to
# ``POST /auto`` and Copilot answers with the model it picked. The tier is part
# of the model id so every layer that keys on the id (Portal picker, runtime
# profile, this proxy) needs no extra field: ``auto`` is the picker's Balance
# default, the two others are VS Code's selectable alternatives ("fast", the
# inline-chat tier, is not offered).
DEFAULT_AUTO_TIER = "balance"
COPILOT_AUTO_TIERS = {
    AUTO_MODEL_ID: DEFAULT_AUTO_TIER,
    "auto-efficiency": "efficiency",
    "auto-intelligence": "intelligence",
}
AUTO_MODEL_IDS = tuple(COPILOT_AUTO_TIERS)
# Sent when auto cannot be routed; the platform default on Copilot.
DEFAULT_AUTO_FALLBACK_MODEL = "gpt-6-sol"
# Copilot models a member can pick (Portal llm_catalog COPILOT_MODELS minus the
# auto ids). Copilot's routing may answer with a model outside this list; that
# pick is admitted on its own metadata (auto_pick_refusal).
SUPPORTED_COPILOT_MODEL_IDS = ("gpt-6-astra", "gpt-6-luna", "gpt-6-sol", "gpt-6.1-sol")
# Copilot's own catalog (GET /models, Business plan, 2026-10-20) reports
# 400,000 tokens with a 272,000-token prompt cap and the 128,000-token output
# cap for the whole GPT-6 line, while models.dev carries OpenAI's 1,050,000.
# The generated opencode.json pins the gateway's numbers so OpenCode compacts
# before Copilot refuses a prompt.
COPILOT_MODEL_LIMITS: dict[str, dict[str, int]] = {
    model: {"context": 400000, "output": 128000} for model in SUPPORTED_COPILOT_MODEL_IDS
}
# What opencode.json declares for the auto ids so OpenCode accepts them. The
# proxy sends the model Copilot picks per turn; the declared limit is the
# fallback model's, and a pick whose prompt cap cannot hold the conversation
# is not admitted (see copilot_auto_session).
COPILOT_AUTO_MODEL_CONFIGS: dict[str, dict[str, Any]] = {
    AUTO_MODEL_ID: {"name": "Auto (Balance)", "limit": {"context": 400000, "output": 128000}},
    "auto-efficiency": {"name": "Auto (Efficiency)", "limit": {"context": 400000, "output": 128000}},
    "auto-intelligence": {"name": "Auto (Intelligence)", "limit": {"context": 400000, "output": 128000}},
}
COPILOT_AUTO_MODEL_CONFIG = COPILOT_AUTO_MODEL_CONFIGS[AUTO_MODEL_ID]

REASONING_EFFORT_LADDER = ("none", "low", "medium", "high", "xhigh", "max")


def _normalize_model_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if "/" in text:
        text = text.split("/", 1)[1]
    text = "-".join(text.split())
    return text or None


def is_auto_model(value: Any) -> bool:
    """Whether a request's ``model`` is an auto id (``auto``, a tier id, or either behind ``github-copilot/``)."""

    return _normalize_model_id(value) in COPILOT_AUTO_TIERS


def auto_tier_for(value: Any) -> str | None:
    """The ``POST /auto`` tier an auto id asks for, or None for a concrete model."""

    model_id = _normalize_model_id(value)
    return COPILOT_AUTO_TIERS.get(model_id) if model_id else None


def _positive_int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int) or value < 1:
        return None
    return value


def copilot_model_limits(entry: Any) -> dict[str, int] | None:
    """``{"context", "prompt", "output"}`` from a Copilot API model entry, or None.

    ``entry`` is a ``GET /models`` row or the ``selected_model`` of a ``POST
    /auto`` answer (``capabilities.limits.max_context_window_tokens`` /
    ``max_prompt_tokens`` / ``max_output_tokens``). Without a prompt cap the
    window minus the output cap is used; without either, three quarters of
    the window.
    """

    if not isinstance(entry, Mapping):
        return None
    capabilities = entry.get("capabilities")
    limits = capabilities.get("limits") if isinstance(capabilities, Mapping) else None
    if not isinstance(limits, Mapping):
        return None
    window = _positive_int_or_none(limits.get("max_context_window_tokens"))
    if window is None:
        return None
    output = _positive_int_or_none(limits.get("max_output_tokens"))
    prompt = _positive_int_or_none(limits.get("max_prompt_tokens"))
    if prompt is None or prompt > window:
        prompt = window - output if output is not None and output < window else window * 3 // 4
    if output is None or output > window:
        output = min(128000, window // 4)
    return {"context": window, "prompt": max(1, prompt), "output": max(1, output)}


def static_model_limits(model: Any) -> dict[str, int] | None:
    """The pinned limits for a model the member can pick, or None."""

    model_id = _normalize_model_id(model)
    pinned = COPILOT_MODEL_LIMITS.get(model_id) if model_id else None
    if pinned is None:
        return None
    return {"context": pinned["context"], "prompt": pinned["context"] - pinned["output"], "output": pinned["output"]}


def smallest_model_limits(candidates: Iterable[Mapping[str, int] | None]) -> dict[str, int] | None:
    """The limits with the smallest prompt cap among those given (None skipped).

    Copilot describes a model in more than one place (the ``/auto`` answer,
    ``GET /models``, the pinned rows) and they do not always agree; the
    smallest cap is the one every description honours.
    """

    best: dict[str, int] | None = None
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        if best is None or int(candidate["prompt"]) < int(best["prompt"]):
            best = dict(candidate)
    return best


def auto_pick_refusal(model: str, *sources: Any) -> str | None:
    """Why this proxy cannot drive the model Copilot picked, or None when it can.

    Judged on the model's metadata (the ``/auto`` answer first, then the
    ``GET /models`` row): a non-chat model, a model not served on
    ``/responses`` (what OpenCode's Copilot provider speaks) or one that
    refuses tool calls cannot run an agent turn.
    """

    for source in sources:
        if not isinstance(source, Mapping):
            continue
        capabilities = source.get("capabilities")
        capabilities = capabilities if isinstance(capabilities, Mapping) else {}
        kind = capabilities.get("type")
        if isinstance(kind, str) and kind.strip() and kind.strip() != "chat":
            return f"selected model {model} is a {kind.strip()} model, not a chat model"
        endpoints = source.get("supported_endpoints")
        if isinstance(endpoints, list) and endpoints:
            served = any(
                isinstance(entry, str) and entry.strip().lstrip("ws:").startswith("/responses")
                for entry in endpoints
            )
            if not served:
                listed = ", ".join(str(entry) for entry in endpoints)
                return f"selected model {model} is not served on /responses (endpoints: {listed})"
        supports = capabilities.get("supports")
        if isinstance(supports, Mapping) and supports.get("tool_calls") is False:
            return f"selected model {model} does not support tool calls"
    return None


def allowed_reasoning_efforts(*sources: Any) -> tuple[str, ...] | None:
    """The reasoning efforts the picked model lists, from the first source that says."""

    for source in sources:
        if not isinstance(source, Mapping):
            continue
        capabilities = source.get("capabilities")
        supports = capabilities.get("supports") if isinstance(capabilities, Mapping) else None
        efforts = supports.get("reasoning_effort") if isinstance(supports, Mapping) else None
        if isinstance(efforts, list):
            allowed = tuple(entry.strip().lower() for entry in efforts if isinstance(entry, str) and entry.strip())
            if allowed:
                return allowed
    return None


def clamp_reasoning_effort(configured: Any, allowed: tuple[str, ...] | None) -> Any:
    """The configured effort, or the nearest one the model lists (lower first)."""

    if not isinstance(configured, str):
        return configured
    effort = configured.strip().lower()
    if not allowed or effort in allowed or effort not in REASONING_EFFORT_LADDER:
        return configured
    index = REASONING_EFFORT_LADDER.index(effort)
    for candidate in reversed(REASONING_EFFORT_LADDER[:index]):
        # ``none`` is never sent on the runtime's behalf.
        if candidate in allowed and candidate != "none":
            return candidate
    for candidate in REASONING_EFFORT_LADDER[index + 1 :]:
        if candidate in allowed:
            return candidate
    return configured


__all__ = [
    "AUTO_MODEL_ID",
    "AUTO_MODEL_IDS",
    "COPILOT_AUTO_MODEL_CONFIG",
    "COPILOT_AUTO_MODEL_CONFIGS",
    "COPILOT_AUTO_TIERS",
    "COPILOT_MODEL_LIMITS",
    "DEFAULT_AUTO_FALLBACK_MODEL",
    "DEFAULT_AUTO_TIER",
    "REASONING_EFFORT_LADDER",
    "SUPPORTED_COPILOT_MODEL_IDS",
    "allowed_reasoning_efforts",
    "auto_pick_refusal",
    "auto_tier_for",
    "clamp_reasoning_effort",
    "copilot_model_limits",
    "is_auto_model",
    "smallest_model_limits",
    "static_model_limits",
]
