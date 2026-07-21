from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .index_loader import load_skills_index
from .permission_generator import build_permission
from .settings import Settings

MANAGED_TOP_LEVEL_KEYS = {"permission", "agent", "server", "autoupdate", "share", "provider", "instructions", "_efp_managed"}
EFP_WORKSPACE_INSTRUCTIONS_GLOB = ".efp/instructions/*.instructions.md"

COPILOT_RESPONSES_PROVIDER_NPM = "@ai-sdk/openai"
COPILOT_PROXY_API_KEY_PLACEHOLDER = "efp-copilot-proxy"
AI_PLATFORM_PROVIDER_NPM = "@ai-sdk/openai-compatible"
AI_PLATFORM_PROXY_API_KEY_PLACEHOLDER = "efp-ai-platform-proxy"


def normalize_opencode_provider_id(provider: str | None) -> str:
    raw = str(provider or "").strip().lower()
    aliases = {
        "github": "github-copilot",
        "copilot": "github-copilot",
        "github_copilot": "github-copilot",
        "github-copilot": "github-copilot",
        "ai_platform": "ai-platform",
        "ai-platform": "ai-platform",
        "ai platform": "ai-platform",
        "claude": "anthropic",
        "anthropic": "anthropic",
        "openai": "openai",
    }
    return aliases.get(raw, raw)


def _ai_platform_credential_present(llm: dict) -> bool:
    ap = llm.get("ai_platform") if isinstance(llm.get("ai_platform"), dict) else None
    if not isinstance(ap, dict):
        return False
    auth = ap.get("auth") if isinstance(ap.get("auth"), dict) else {}
    if _clean_string(auth.get("token")):
        return True
    # Username/password alone can't authenticate — the iB2B exchange also needs
    # an endpoint. Match what exchange_ai_platform_token actually requires.
    ib2b = ap.get("ib2b") if isinstance(ap.get("ib2b"), dict) else {}
    return bool(
        _clean_string(auth.get("username"))
        and _clean_string(auth.get("password"))
        and _clean_string(ib2b.get("host"))
    )


def model_from_runtime_profile(config: dict) -> str | None:
    llm = config.get("llm") if isinstance(config, dict) else None
    if not isinstance(llm, dict):
        return None
    provider = llm.get("provider")
    model = llm.get("model")
    if isinstance(model, str) and "/" in model:
        prefix, suffix = model.split("/", 1)
        return f"{normalize_opencode_provider_id(prefix)}/{suffix}"
    if provider and model:
        return f"{normalize_opencode_provider_id(provider)}/{model}"
    return None


def _int_or_none(value: object) -> int | None:
    try:
        parsed = int(str(value))
    except Exception:
        return None
    return parsed if parsed > 0 else None


def _clean_string(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _copilot_credential_present(llm: dict) -> bool:
    oauth = llm.get("oauth") if isinstance(llm.get("oauth"), dict) else None
    if isinstance(oauth, dict) and (_clean_string(oauth.get("refresh")) or _clean_string(oauth.get("access"))):
        return True
    return bool(_clean_string(llm.get("api_key")))


def provider_config_from_runtime_profile(runtime_config: dict, settings: Settings | None = None) -> dict:
    llm = runtime_config.get("llm") if isinstance(runtime_config.get("llm"), dict) else {}
    provider = normalize_opencode_provider_id(llm.get("provider"))
    if not provider:
        model = llm.get("model")
        if isinstance(model, str) and "/" in model:
            provider = normalize_opencode_provider_id(model.split("/", 1)[0])
    if not provider:
        return {}
    options: dict[str, object] = {}
    base_url = llm.get("base_url") or llm.get("api_base") or llm.get("baseURL") or llm.get("endpoint")
    if provider == "github-copilot" and _copilot_credential_present(llm):
        proxy_base_url = (settings.copilot_proxy_base_url if settings else Settings.from_env().copilot_proxy_base_url).strip().rstrip("/")
        if proxy_base_url:
            options["baseURL"] = proxy_base_url
    elif provider == "ai-platform" and _ai_platform_credential_present(llm):
        proxy_base_url = (settings.ai_platform_proxy_base_url if settings else Settings.from_env().ai_platform_proxy_base_url).strip().rstrip("/")
        if proxy_base_url:
            options["baseURL"] = proxy_base_url
    elif isinstance(base_url, str) and base_url.strip():
        options["baseURL"] = base_url.strip().rstrip("/")
    timeout_ms = _int_or_none(llm.get("timeout_ms") or llm.get("timeout"))
    if timeout_ms:
        options["timeout"] = timeout_ms
    chunk_timeout_ms = _int_or_none(llm.get("chunk_timeout_ms") or llm.get("chunkTimeout"))
    if chunk_timeout_ms:
        options["chunkTimeout"] = chunk_timeout_ms
    if provider == "github-copilot" and options.get("baseURL"):
        # GitHub Copilot GPT-5.4 class models require the OpenAI Responses API
        # when function tools/reasoning options are present. OpenCode's
        # openai-compatible provider uses /chat/completions; @ai-sdk/openai
        # selects /responses for these models. The local proxy strips inbound
        # Authorization and replaces it with the internal Copilot token.
        options.setdefault("apiKey", COPILOT_PROXY_API_KEY_PLACEHOLDER)
        return {"provider": {provider: {"npm": COPILOT_RESPONSES_PROVIDER_NPM, "options": options}}}
    if provider == "ai-platform" and options.get("baseURL"):
        # AI Platform is an OpenAI-compatible /chat/completions endpoint behind a
        # local loopback proxy that injects a fresh short-lived trust token per
        # request. The apiKey is a placeholder; the proxy supplies the real auth.
        options.setdefault("apiKey", AI_PLATFORM_PROXY_API_KEY_PLACEHOLDER)
        return {"provider": {provider: {"npm": AI_PLATFORM_PROVIDER_NPM, "options": options}}}
    if not options:
        return {}
    return {"provider": {provider: {"options": options}}}


def build_opencode_config(settings: Settings, runtime_config: dict | None = None) -> tuple[dict, str, list[str]]:
    runtime_config = runtime_config if isinstance(runtime_config, dict) else {}
    skills_index = load_skills_index(settings)
    permission = build_permission(runtime_config, skills_index=skills_index, permission_mode=settings.opencode_permission_mode, allow_bash_all=settings.opencode_allow_bash_all)
    generated = {
        "$schema": "https://opencode.ai/config.json",
        "autoupdate": False,
        "share": "disabled",
        "server": {"hostname": "127.0.0.1", "port": 4096},
        "permission": permission,
        "instructions": [EFP_WORKSPACE_INSTRUCTIONS_GLOB],
        "agent": {
            "efp-main": {
                "description": "Portal managed OpenCode primary agent",
                "mode": "primary",
                "steps": 40,
                "permission": {},
            }
        },
    }
    updated = ["permission", "agent", "instructions"]
    model = model_from_runtime_profile(runtime_config)
    if model:
        generated["agent"]["efp-main"]["model"] = model
        updated.append("llm")
    provider_patch = provider_config_from_runtime_profile(runtime_config, settings)
    if provider_patch:
        generated.setdefault("provider", {}).update(provider_patch["provider"])
        updated.append("provider")
    digest_src = json.dumps(generated, sort_keys=True, separators=(",", ":"))
    return generated, hashlib.sha256(digest_src.encode("utf-8")).hexdigest(), updated


def write_opencode_config(settings: Settings, config: dict) -> None:
    path = settings.opencode_config_path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = Path(f"{path}.tmp")
    tmp_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def _hash_index_payload(payload: dict) -> str:
    src = json.dumps(payload if isinstance(payload, dict) else {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(src.encode("utf-8")).hexdigest()


def merge_with_existing_config(existing: dict | None, generated: dict, *, skills_index: dict) -> dict:
    merged = dict(existing) if isinstance(existing, dict) else {}
    for key, value in generated.items():
        if key in MANAGED_TOP_LEVEL_KEYS:
            merged[key] = value
    merged["_efp_managed"] = {
        "skills_index_hash": _hash_index_payload(skills_index),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    return merged
