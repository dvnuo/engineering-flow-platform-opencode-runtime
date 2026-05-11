from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from .index_loader import load_skills_index, read_json_file
from .permission_generator import default_permission_baseline, skill_permission_state
from .settings import Settings
from .skill_sync import normalize_skill_name

SLASH_RE = re.compile(r"^/([A-Za-z0-9][A-Za-z0-9_-]*)(?:\s+(.*))?$")

_WRITEBACK_TOOL_NAMES = {"github_create_pull_request", "efp_github_create_pull_request"}
_WRITEBACK_POLICY_TAGS = {"mutation", "write", "requires_approval"}


def _missing_required_writeback_tools(skill: dict[str, Any]) -> bool:
    missing_tools = {str(x) for x in (skill.get("missing_tools") or []) if isinstance(x, str)}
    missing_opencode_tools = {str(x) for x in (skill.get("missing_opencode_tools") or []) if isinstance(x, str)}
    if _WRITEBACK_TOOL_NAMES & (missing_tools | missing_opencode_tools):
        return True
    mappings = skill.get("tool_mappings") if isinstance(skill.get("tool_mappings"), list) else []
    for mapping in mappings:
        if not isinstance(mapping, dict) or mapping.get("available") is not False:
            continue
        efp_name = str(mapping.get("efp_name") or "")
        opencode_name = str(mapping.get("opencode_name") or "")
        if efp_name in _WRITEBACK_TOOL_NAMES or opencode_name in _WRITEBACK_TOOL_NAMES:
            return True
        tags = mapping.get("policy_tags") if isinstance(mapping.get("policy_tags"), list) else []
        if {str(t).lower() for t in tags} & _WRITEBACK_POLICY_TAGS:
            return True
    skill_tags = skill.get("policy_tags") if isinstance(skill.get("policy_tags"), list) else []
    return bool({str(t).lower() for t in skill_tags} & _WRITEBACK_POLICY_TAGS and (missing_tools or missing_opencode_tools))


@dataclass(frozen=True)
class SlashInvocation:
    raw_name: str
    skill_name: str
    arguments: str


@dataclass(frozen=True)
class SkillDecision:
    skill: dict[str, Any] | None
    allowed: bool
    reason: str
    permission_state: str


def parse_slash_invocation(message: str) -> SlashInvocation | None:
    if not isinstance(message, str) or not message.strip():
        return None
    stripped = message.strip()
    if "\n" in stripped:
        lines = stripped.splitlines()
        if len(lines) != 1:
            return None
        stripped = lines[0].strip()
    match = SLASH_RE.match(stripped)
    if not match:
        return None
    raw_name = match.group(1)
    skill_name = normalize_skill_name(raw_name, raw_name)
    arguments = (match.group(2) or "").strip()
    return SlashInvocation(raw_name=raw_name, skill_name=skill_name, arguments=arguments)


def resolve_skill(settings: Settings, name: str) -> dict[str, Any] | None:
    skills = load_skills_index(settings).get("skills", [])
    if not isinstance(skills, list):
        return None
    input_canonical = normalize_skill_name(name, name)
    aliases = {name.lower(), name.replace("_", "-").lower(), input_canonical}
    for skill in skills:
        if not isinstance(skill, dict):
            continue
        efp_name = str(skill.get("efp_name") or "")
        opencode_name = str(skill.get("opencode_name") or "")
        candidates: set[str] = set()
        for value in (opencode_name, efp_name):
            if not value:
                continue
            candidates.add(value.lower())
            candidates.add(value.replace("_", "-").lower())
            candidates.add(normalize_skill_name(value, value))
        if aliases & candidates:
            return skill
    return None


def load_skill_permission(settings: Settings) -> dict[str, Any]:
    cfg = read_json_file(settings.opencode_config_path) or {}
    if isinstance(cfg.get("permission"), dict):
        return cfg["permission"]
    return default_permission_baseline()


def evaluate_skill_invocation(settings: Settings, invocation: SlashInvocation) -> SkillDecision:
    skill = resolve_skill(settings, invocation.skill_name)
    if not skill:
        return SkillDecision(skill=None, allowed=False, reason="unknown_skill", permission_state="unknown")
    if skill.get("opencode_supported") is False:
        return SkillDecision(skill=skill, allowed=False, reason="unsupported_for_opencode", permission_state="unknown")
    permission_state = skill_permission_state(load_skill_permission(settings), str(skill.get("opencode_name") or invocation.skill_name))
    if permission_state in {"denied", "unknown"}:
        return SkillDecision(skill=skill, allowed=False, reason="permission_denied", permission_state=permission_state)
    if bool(skill.get("programmatic")) and not bool(skill.get("runtime_equivalence")):
        return SkillDecision(skill=skill, allowed=False, reason="programmatic_skill_requires_opencode_wrapper", permission_state=permission_state)
    if skill.get("missing_tools") or skill.get("missing_opencode_tools"):
        if _missing_required_writeback_tools(skill):
            return SkillDecision(skill=skill, allowed=False, reason="missing_required_writeback_tools", permission_state=permission_state)
        return SkillDecision(
            skill=skill,
            allowed=True,
            reason="allowed_with_missing_tools",
            permission_state=permission_state,
        )
    return SkillDecision(skill=skill, allowed=True, reason="allowed", permission_state=permission_state)


def build_skill_prompt(skill: dict[str, Any], invocation: SlashInvocation) -> str:
    skill_name = str(skill.get("opencode_name") or invocation.skill_name)
    raw = f"/{invocation.raw_name} {invocation.arguments}".strip()
    missing_tools = skill.get("missing_tools") if isinstance(skill.get("missing_tools"), list) else []
    missing_opencode_tools = skill.get("missing_opencode_tools") if isinstance(skill.get("missing_opencode_tools"), list) else []

    compatibility_warning = ""
    if missing_tools or missing_opencode_tools:
        compatibility_warning = (
            "\n\nCompatibility warning:\n"
            "- This skill has EFP-declared tools that are not currently mapped to OpenCode wrappers.\n"
            f"- Missing EFP tools: {', '.join(str(tool) for tool in missing_tools) if missing_tools else '(none)'}\n"
            f"- Missing declared OpenCode tools: {', '.join(str(tool) for tool in missing_opencode_tools) if missing_opencode_tools else '(none)'}\n"
            "- Still load and apply the skill as far as possible using available OpenCode tools.\n"
            "- If a specific step requires an unavailable tool, explain the exact blocker and continue with any useful partial result.\n"
            "- Do not replace missing writeback/API tools with raw curl or ungoverned shell/API calls."
        )

    return (
        f"Run the OpenCode agent skill `{skill_name}`.\n\n"
        "Original user slash command:\n"
        f"`{raw}`\n\n"
        "Arguments:\n"
        f"{invocation.arguments}\n\n"
        "Instructions:\n"
        f"1. Use the native OpenCode `skill` tool to load skill `{skill_name}`.\n"
        f"2. Follow `.opencode/skills/{skill_name}/SKILL.md`.\n"
        "3. Do not claim that the skill is running unless you have actually loaded and applied it.\n"
        "4. If the skill cannot be loaded, or required tools are unavailable, report the exact blocker."
        f"{compatibility_warning}"
    )
