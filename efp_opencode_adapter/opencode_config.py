from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .index_loader import load_skills_index, load_tools_index
from .permission_generator import build_permission
from .settings import Settings


def model_from_runtime_profile(config: dict) -> str | None:
    llm = config.get("llm") if isinstance(config, dict) else None
    if not isinstance(llm, dict):
        return None
    provider = llm.get("provider")
    model = llm.get("model")
    if provider and model:
        return f"{provider}/{model}"
    return None


def write_main_agent_prompt(settings: Settings) -> Path:
    path = settings.workspace_dir / ".opencode" / "agents" / "efp-main.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "This runtime is managed by EFP Portal.",
                "Obey Portal capability/profile/policy metadata.",
                "Do not write back to external systems unless explicitly allowed.",
                "Use efp_* tools for Jira/GitHub/Confluence rather than raw curl when available.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def build_opencode_config(settings: Settings, runtime_config: dict | None = None) -> tuple[dict, str, list[str]]:
    runtime_config = runtime_config if isinstance(runtime_config, dict) else {}
    skills_index = load_skills_index(settings)
    tools_index = load_tools_index(settings)
    permission = build_permission(runtime_config, skills_index=skills_index, tools_index=tools_index)
    generated = {
        "$schema": "https://opencode.ai/config.json",
        "autoupdate": False,
        "share": "disabled",
        "server": {"hostname": "127.0.0.1", "port": 4096},
        "permission": permission,
        "agent": {
            "efp-main": {
                "description": "Portal managed OpenCode primary agent",
                "mode": "primary",
                "prompt": "{file:/workspace/.opencode/agents/efp-main.md}",
                "steps": 40,
                "permission": {},
            }
        },
    }
    updated = ["permission", "agent"]
    model = model_from_runtime_profile(runtime_config)
    if model:
        generated["agent"]["efp-main"]["model"] = model
        updated.append("llm")
    digest_src = json.dumps(generated, sort_keys=True, separators=(",", ":"))
    return generated, hashlib.sha256(digest_src.encode("utf-8")).hexdigest(), updated


def write_opencode_config(settings: Settings, config: dict) -> None:
    path = settings.opencode_config_path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = Path(f"{path}.tmp")
    tmp_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)
