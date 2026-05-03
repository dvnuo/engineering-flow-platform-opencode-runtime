from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from .opencode_config import read_json_file
from .profile_store import redact_secrets
from .settings import Settings

BUILTIN_CAPABILITIES = [
    {"capability_id": "opencode.builtin.read", "type": "tool", "name": "read", "description": "Read file content", "enabled": True, "policy_tags": ["filesystem", "read_only"], "source_ref": "opencode"},
    {"capability_id": "opencode.builtin.glob", "type": "tool", "name": "glob", "description": "Find files by glob pattern", "enabled": True, "policy_tags": ["filesystem", "read_only"], "source_ref": "opencode"},
    {"capability_id": "opencode.builtin.grep", "type": "tool", "name": "grep", "description": "Search text in files", "enabled": True, "policy_tags": ["filesystem", "read_only"], "source_ref": "opencode"},
    {"capability_id": "opencode.builtin.edit", "type": "tool", "name": "edit", "description": "Edit file content", "enabled": True, "policy_tags": ["filesystem", "mutation"], "source_ref": "opencode"},
    {"capability_id": "opencode.builtin.write", "type": "tool", "name": "write", "description": "Write file content", "enabled": True, "policy_tags": ["filesystem", "mutation"], "source_ref": "opencode"},
    {"capability_id": "opencode.builtin.bash", "type": "tool", "name": "bash", "description": "Run shell command", "enabled": True, "policy_tags": ["shell", "mutation"], "source_ref": "opencode"},
    {"capability_id": "opencode.builtin.skill", "type": "tool", "name": "skill", "description": "Invoke skill", "enabled": True, "policy_tags": ["skill"], "source_ref": "opencode"},
    {"capability_id": "opencode.builtin.todowrite", "type": "tool", "name": "todowrite", "description": "Update planning todo", "enabled": True, "policy_tags": ["planning", "mutation"], "source_ref": "opencode"},
    {"capability_id": "opencode.builtin.webfetch", "type": "tool", "name": "webfetch", "description": "Fetch web page", "enabled": True, "policy_tags": ["web", "read_only"], "source_ref": "opencode"},
    {"capability_id": "opencode.builtin.websearch", "type": "tool", "name": "websearch", "description": "Search web", "enabled": True, "policy_tags": ["web", "read_only"], "source_ref": "opencode"},
    {"capability_id": "opencode.builtin.question", "type": "tool", "name": "question", "description": "Request user interaction", "enabled": True, "policy_tags": ["user_interaction"], "source_ref": "opencode"},
]

def load_skills_capabilities(settings: Settings) -> list[dict[str, Any]]:
    data = read_json_file(settings.adapter_state_dir / "skills-index.json") or {}
    out = []
    for item in data.get("skills", []):
        if not isinstance(item, dict) or not item.get("opencode_name"):
            continue
        tags = ["skill"]
        if item.get("risk_level"):
            tags.append(item["risk_level"])
        out.append({"capability_id": f"opencode.skill.{item['opencode_name']}", "type": "skill", "name": item["opencode_name"], "description": item.get("description", ""), "enabled": True, "policy_tags": tags, "source_ref": "skills_repo", "metadata": {"efp_name": item.get("efp_name"), "tools": item.get("tools", []), "task_tools": item.get("task_tools", [])}})
    return out

def load_tools_capabilities(settings: Settings) -> list[dict[str, Any]]:
    paths = [
        settings.adapter_state_dir / "tools-index.json",
        settings.workspace_dir / ".opencode" / "tools-index.json",
        settings.tools_dir / "tools-index.json",
        settings.tools_dir / "manifest.json",
    ]
    data = None
    for p in paths:
        data = read_json_file(p)
        if data:
            break
    if not data:
        return []
    items = data.get("tools") if isinstance(data.get("tools"), list) else data.get("capabilities") if isinstance(data.get("capabilities"), list) else []
    out = []
    for t in items:
        if not isinstance(t, dict):
            continue
        cap_id = t.get("capability_id") or t.get("tool_id") or t.get("action_id")
        name = t.get("opencode_name") or t.get("name")
        if not cap_id or not name:
            continue
        out.append({"capability_id": cap_id, "type": t.get("type", "adapter_action"), "name": name, "description": t.get("description", ""), "policy_tags": t.get("policy_tags", []), "source_ref": t.get("source_ref", "tools_repo"), "enabled": t.get("enabled", True), "input_schema": t.get("input_schema"), "output_schema": t.get("output_schema"), "requires_identity_binding": t.get("requires_identity_binding")})
    return out

def load_agent_capabilities(settings: Settings) -> list[dict[str, Any]]:
    cfg = read_json_file(settings.opencode_config_path) or {}
    agents = cfg.get("agent") if isinstance(cfg.get("agent"), dict) else {}
    return [{"capability_id": f"opencode.agent.{name}", "type": "agent", "name": name, "description": (meta.get("description") if isinstance(meta, dict) and meta.get("description") else f"OpenCode agent {name}"), "enabled": True, "policy_tags": ["agent"], "source_ref": "opencode_config"} for name, meta in agents.items()]


async def build_capability_catalog(settings: Settings, opencode_client=None) -> dict:
    capabilities = [*BUILTIN_CAPABILITIES, *load_tools_capabilities(settings), *load_skills_capabilities(settings), *load_agent_capabilities(settings)]
    if opencode_client and hasattr(opencode_client, "mcp"):
        try:
            mcp_data = await opencode_client.mcp()
            if isinstance(mcp_data, dict) and isinstance(mcp_data.get("tools"), list):
                for item in mcp_data["tools"]:
                    if isinstance(item, dict):
                        capabilities.append(redact_secrets(item))
        except Exception:
            pass
    capabilities = [redact_secrets(item) for item in capabilities]
    digest = hashlib.sha256(json.dumps(capabilities, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"capabilities": capabilities, "count": len(capabilities), "catalog_version": digest, "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "supports_snapshot_contract": True, "runtime_contract_version": "efp-opencode-compat-v1"}
