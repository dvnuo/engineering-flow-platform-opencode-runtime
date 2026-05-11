from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from .index_loader import load_skills_index, load_tools_index, read_json_file
from .permission_generator import default_permission_baseline, skill_permission_state
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
    cfg = read_json_file(settings.opencode_config_path) or {}
    permission = cfg.get("permission") if isinstance(cfg.get("permission"), dict) else default_permission_baseline()
    out = []
    for item in data.get("skills", []):
        if not isinstance(item, dict) or not item.get("opencode_name"):
            continue
        tags = ["skill"]
        if item.get("risk_level"):
            tags.append(item["risk_level"])
        state = skill_permission_state(permission, item["opencode_name"])
        supported = bool(item.get("opencode_supported", True))
        callable = state in {"allowed", "ask"} and supported
        blocked = "skill is not supported for OpenCode runtime" if not supported else ("skill denied by current OpenCode permission profile" if state == "denied" else None)
        compat_payload = {
            "opencode_compatibility": item.get("opencode_compatibility", "prompt_only"),
            "runtime_equivalence": bool(item.get("runtime_equivalence", True)),
            "programmatic": bool(item.get("programmatic", False)),
            "opencode_supported": supported,
            "compatibility_warnings": item.get("compatibility_warnings", []),
            "tool_mappings": item.get("tool_mappings", []),
            "opencode_tools": item.get("opencode_tools", []),
            "missing_tools": item.get("missing_tools", []),
            "missing_opencode_tools": item.get("missing_opencode_tools", []),
        }
        out.append({"capability_id": f"opencode.skill.{item['opencode_name']}", "type": "skill", "name": item["opencode_name"], "description": item.get("description", ""), "enabled": True, "policy_tags": tags, "source_ref": "skills_repo", "permission_state": state, "callable": callable, "blocked_reason": blocked, **compat_payload, "metadata": _drop_none({"efp_name": item.get("efp_name"), "tools": item.get("tools", []), "task_tools": item.get("task_tools", []), "permission_state": state, "callable": callable, **compat_payload})})
    return out


def load_tools_capabilities(settings: Settings) -> list[dict[str, Any]]:
    return []


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
    return {"engine": "opencode", "capabilities": capabilities, "count": len(capabilities), "catalog_version": digest, "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "supports_snapshot_contract": True, "runtime_contract_version": "efp-opencode-compat-v1"}
