"""Copilot "auto" constants and pure helpers shared by the config generator and the proxy.

Kept free of package imports so ``opencode_config`` (which the Copilot auth
module's import chain reaches) and ``copilot_auto_session`` can both use it.
Mirrors the native runtime's ``efp_runtime/llm/models.py`` (AUTO_MODEL_ID,
COPILOT_AUTO_PREFERENCE, select_auto_model); keep them aligned.
"""
from __future__ import annotations

from typing import Any, Iterable

AUTO_MODEL_ID = "auto"
# Sent when auto cannot be routed; the platform default on Copilot.
DEFAULT_AUTO_FALLBACK_MODEL = "gpt-6-sol"
# Copilot models the EFP runtimes drive (Portal llm_catalog COPILOT_MODELS minus auto).
SUPPORTED_COPILOT_MODEL_IDS = ("gpt-6-astra", "gpt-6-luna", "gpt-6-sol", "gpt-6.1-sol")
# Pick order within the auto session's pool; first available wins.
COPILOT_AUTO_PREFERENCE = ("gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna", "gpt-6-astra")
# What opencode.json declares for the auto model so OpenCode accepts the id;
# the smallest window of the models auto may pick (all GPT-6: 1,050,000).
COPILOT_AUTO_MODEL_CONFIG: dict[str, Any] = {
    "name": "Auto (Copilot picks a GPT-6 model)",
    "limit": {"context": 1050000, "output": 128000},
}


def _normalize_model_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if "/" in text:
        text = text.split("/", 1)[1]
    text = "-".join(text.split())
    return text or None


def is_auto_model(value: Any) -> bool:
    """Whether a request's ``model`` names Copilot auto (``auto`` or ``github-copilot/auto``)."""

    return _normalize_model_id(value) == AUTO_MODEL_ID


def select_auto_model(
    available_models: Iterable[Any] | None,
    *,
    preference: tuple[str, ...] = COPILOT_AUTO_PREFERENCE,
) -> str | None:
    """First preferred model present in the pool, else any supported one, else None.

    Models the runtime cannot drive (anything outside SUPPORTED_COPILOT_MODEL_IDS,
    such as the Claude, Gemini and MAI entries Copilot's pool also lists) are
    never chosen.
    """

    pool: set[str] = set()
    for entry in available_models or ():
        model_id = _normalize_model_id(entry)
        if model_id:
            pool.add(model_id)
    for candidate in preference:
        if candidate in pool and candidate in SUPPORTED_COPILOT_MODEL_IDS:
            return candidate
    for candidate in SUPPORTED_COPILOT_MODEL_IDS:
        if candidate in pool:
            return candidate
    return None


__all__ = [
    "AUTO_MODEL_ID",
    "COPILOT_AUTO_MODEL_CONFIG",
    "COPILOT_AUTO_PREFERENCE",
    "DEFAULT_AUTO_FALLBACK_MODEL",
    "SUPPORTED_COPILOT_MODEL_IDS",
    "is_auto_model",
    "select_auto_model",
]
