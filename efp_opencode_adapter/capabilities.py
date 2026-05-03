from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from .index_loader import load_skills_index, load_tools_index, read_json_file
from .profile_store import sanitize_public_secrets
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


def _drop_none(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if v is not None}


def load_skills_capabilities(settings: Settings) -> list[dict[str, Any]]:
    data = load_skills_index(settings)
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
    out = []
    for t in load_tools_index(settings).get("tools", []):
        if not isinstance(t, dict):
            continue
        out.append(
            _drop_none(
                {
                    "capability_id": t.get("capability_id"),
                    "type": t.get("type", "adapter_action"),
                    "name": t.get("name"),
                    "description": t.get("description", ""),
                    "policy_tags": t.get("policy_tags", []),
                    "source_ref": t.get("source_ref", "tools_repo"),
                    "enabled": t.get("enabled", True),
                    "input_schema": t.get("input_schema"),
                    "output_schema": t.get("output_schema"),
                    "requires_identity_binding": t.get("requires_identity_binding"),
                }
            )
        )
    return out


def load_agent_capabilities(settings: Settings) -> list[dict[str, Any]]:
    cfg = read_json_file(settings.opencode_config_path) or {}
    agents = cfg.get("agent") if isinstance(cfg.get("agent"), dict) else {}
    return [{"capability_id": f"opencode.agent.{name}", "type": "agent", "name": name, "description": (meta.get("description") if isinstance(meta, dict) and meta.get("description") else f"OpenCode agent {name}"), "enabled": True, "policy_tags": ["agent"], "source_ref": "opencode_config"} for name, meta in agents.items()]


def normalize_mcp_capability(item: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    name = item.get("name")
    cap_id = item.get("capability_id")
    if not cap_id and not name:
        return None
    if not cap_id and name:
        safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", str(name)).strip("_").lower() or "tool"
        cap_id = f"opencode.mcp.{safe}"
    tags = ["mcp"] + [str(x) for x in (item.get("policy_tags") or [])]
    cap = {
        "capability_id": str(cap_id),
        "type": item.get("type", "mcp_tool"),
        "name": str(name or cap_id),
        "description": item.get("description", ""),
        "enabled": bool(item.get("enabled", True)),
        "policy_tags": tags,
        "source_ref": item.get("source_ref", "opencode_mcp"),
        "input_schema": item.get("input_schema") or item.get("inputSchema"),
        "output_schema": item.get("output_schema") or item.get("outputSchema"),
    }
    return _drop_none(cap)


async def build_capability_catalog(settings: Settings, opencode_client=None) -> dict:
    capabilities = [*BUILTIN_CAPABILITIES, *load_tools_capabilities(settings), *load_skills_capabilities(settings), *load_agent_capabilities(settings)]
    if opencode_client and hasattr(opencode_client, "mcp"):
        try:
            mcp_data = await opencode_client.mcp()
            if isinstance(mcp_data, dict) and isinstance(mcp_data.get("tools"), list):
                for item in mcp_data["tools"]:
                    cap = normalize_mcp_capability(item) if isinstance(item, dict) else None
                    if cap:
                        capabilities.append(cap)
        except Exception:
            pass
    sanitized = []
    for item in capabilities:
        clean = sanitize_public_secrets(item)
        if not isinstance(clean, dict):
            continue
        if clean.get("capability_id") == "[redacted]" or clean.get("name") == "[redacted]":
            continue
        sanitized.append(clean)
    capabilities = sanitized
    digest = hashlib.sha256(json.dumps(capabilities, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"capabilities": capabilities, "count": len(capabilities), "catalog_version": digest, "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "supports_snapshot_contract": True, "runtime_contract_version": "efp-opencode-compat-v1"}
