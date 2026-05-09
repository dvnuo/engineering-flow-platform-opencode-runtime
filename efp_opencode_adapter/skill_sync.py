from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import warnings
from dataclasses import asdict, dataclass, field
from typing import Any
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .settings import Settings

KNOWN_FIELDS = {
    "name",
    "description",
    "version",
    "owner",
    "triggers",
    "tools",
    "task_tools",
    "risk_level",
    "output_format",
    "when_to_use",
    "model",
    "hooks",
    "kind", "runtime", "execution", "runtime_compat", "opencode_supported", "opencode", "tool_mapping", "opencode_tools", "opencode_runtime_equivalence",
}

SUBAGENT_ALLOWLIST = {"review-pull-request", "create-pull-request"}
SKIP_DIRS = {".git", ".github", "scripts", "__pycache__"}
GENERATED_MARKER = "This skill was generated from an EFP skill asset."
GENERATED_COMMAND_MARKER = "This command was generated from an EFP skill asset."


@dataclass(frozen=True)
class SkillIndexEntry:
    efp_name: str
    opencode_name: str
    description: str
    tools: list[str]
    task_tools: list[str]
    risk_level: str | None
    source_path: str
    target_path: str
    opencode_compatibility: str = "prompt_only"
    runtime_equivalence: bool = True
    programmatic: bool = False
    opencode_supported: bool = True
    compatibility_warnings: list[str] = field(default_factory=list)
    tool_mappings: list[dict[str, Any]] = field(default_factory=list)
    opencode_tools: list[str] = field(default_factory=list)
    missing_tools: list[str] = field(default_factory=list)
    missing_opencode_tools: list[str] = field(default_factory=list)


def _mapping_display_name(mapping: dict[str, Any]) -> str:
    if mapping.get("efp_name"):
        return str(mapping["efp_name"])
    if mapping.get("opencode_name"):
        return f"[declared opencode tool] {mapping['opencode_name']}"
    return "[unknown tool]"


@dataclass(frozen=True)
class SkillsIndex:
    generated_at: str
    skills: list[SkillIndexEntry]
    warnings: list[str] = field(default_factory=list)

    def to_json_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "skills": [asdict(x) for x in self.skills],
            "warnings": list(self.warnings),
        }


def _split_frontmatter(text: str) -> tuple[dict, str] | None:
    if not text.startswith("---\n"):
        return None
    marker = "\n---\n"
    end_idx = text.find(marker, 4)
    if end_idx == -1:
        return None
    fm_raw = text[4:end_idx]
    body = text[end_idx + len(marker) :]
    parsed = yaml.safe_load(fm_raw) or {}
    if not isinstance(parsed, dict):
        raise ValueError("frontmatter must be a mapping")
    return parsed, body


def _validate_list_field(frontmatter: dict, source_path: Path, field_name: str) -> list[str]:
    value = frontmatter.get(field_name, [])
    if value is None:
        return []
    if isinstance(value, list):
        if not all(isinstance(x, str) for x in value):
            raise ValueError(f"{source_path}: {field_name} must be list[str]")
        return value
    if value == "":
        return []
    raise ValueError(f"{source_path}: {field_name} must be list[str], got {type(value).__name__}")


def normalize_skill_name(raw_name: str, fallback_seed: str) -> str:
    normalized = raw_name.lower()
    normalized = normalized.replace("_", "-")
    normalized = re.sub(r"\s+", "-", normalized)
    normalized = re.sub(r"[^a-z0-9-]", "-", normalized)
    normalized = re.sub(r"-+", "-", normalized)
    normalized = normalized.strip("-")
    if not normalized:
        normalized = f"skill-{hashlib.sha256(fallback_seed.encode('utf-8')).hexdigest()[:12]}"
    return normalized


def _discover_skill_files(skills_dir: Path) -> list[Path]:
    discovered: list[Path] = []
    for child in skills_dir.iterdir():
        if not child.is_dir():
            continue
        if child.name in SKIP_DIRS or child.name.startswith("."):
            continue
        candidate = child / "skill.md"
        if candidate.exists():
            discovered.append(candidate)

    for md in skills_dir.glob("*.md"):
        parsed = _split_frontmatter(md.read_text(encoding="utf-8"))
        if parsed is not None:
            discovered.append(md)

    return sorted(discovered, key=lambda p: str(p))




def _is_programmatic_skill(source_path: Path, frontmatter: dict[str, Any]) -> bool:
    op = frontmatter.get("opencode") if isinstance(frontmatter.get("opencode"), dict) else {}
    return any([
        (source_path.parent / "skill.py").exists(),
        source_path.with_suffix(".py").exists(),
        str(frontmatter.get("kind", "")).lower() in {"programmatic", "hybrid"},
        str(frontmatter.get("execution", "")).lower() == "python",
        str(frontmatter.get("runtime", "")).lower() == "python",
        str(op.get("execution", "")).lower() == "python",
    ])


def _opencode_supported(frontmatter: dict[str, Any]) -> bool:
    if frontmatter.get("opencode_supported") is False:
        return False
    rc = frontmatter.get("runtime_compat")
    if isinstance(rc, list):
        values = {str(x).lower() for x in rc}
        if not ({"opencode", "all"} & values):
            return False
    if isinstance(rc, str) and rc.lower() not in {"opencode", "all"}:
        return False
    op = frontmatter.get("opencode") if isinstance(frontmatter.get("opencode"), dict) else {}
    if op.get("compatible") is False or op.get("supported") is False:
        return False
    return True


def _has_explicit_opencode_wrapper(frontmatter: dict[str, Any]) -> bool:
    op = frontmatter.get("opencode") if isinstance(frontmatter.get("opencode"), dict) else {}
    return bool(frontmatter.get("opencode_tools") or frontmatter.get("tool_mapping") or op.get("execution_tool") or op.get("wrapper"))


def _declared_runtime_equivalence(frontmatter: dict[str, Any]) -> str:
    op = frontmatter.get("opencode") if isinstance(frontmatter.get("opencode"), dict) else {}
    value = op.get("runtime_equivalence") or frontmatter.get("opencode_runtime_equivalence")
    return str(value or "").lower()


def _frontmatter_opencode_tools(frontmatter: dict[str, Any]) -> list[str]:
    out: list[str] = []
    op = frontmatter.get("opencode") if isinstance(frontmatter.get("opencode"), dict) else {}
    for raw in (frontmatter.get("opencode_tools"), op.get("tools")):
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, str) and item and item not in out:
                    out.append(item)
    return out


def _frontmatter_tool_mapping(frontmatter: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    raw = frontmatter.get("tool_mapping")
    if isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(k, str) and isinstance(v, str) and k and v:
                out[k] = v
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            efp_name = item.get("efp_name") or item.get("legacy_name")
            op_name = item.get("opencode_name")
            if isinstance(efp_name, str) and isinstance(op_name, str) and efp_name and op_name:
                out[efp_name] = op_name
    return out


def build_tool_name_map(tools_index: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    out = {}
    tools = (tools_index or {}).get("tools", []) if isinstance(tools_index, dict) else []
    for t in tools:
        if not isinstance(t, dict):
            continue
        enabled = bool(t.get("enabled", True))
        base = {
            "legacy_name": t.get("legacy_name"),
            "opencode_name": t.get("opencode_name") or t.get("name"),
            "native_name": t.get("native_name"),
            "efp_name": t.get("efp_name"),
            "tool_id": t.get("tool_id"),
            "capability_id": t.get("capability_id"),
            "policy_tags": t.get("policy_tags", []),
            "enabled": enabled,
            "source_ref": t.get("source_ref"),
            "risk_level": t.get("risk_level"),
            "requires_identity_binding": bool(t.get("requires_identity_binding", False)),
        }
        for k in [t.get("legacy_name"), t.get("native_name"), t.get("efp_name"), t.get("name"), t.get("opencode_name"), t.get("capability_id"), t.get("tool_id")]:
            if isinstance(k, str) and k:
                if k not in out:
                    out[k] = base
                elif not bool(out[k].get("enabled", True)) and bool(base.get("enabled", True)):
                    out[k] = base
    return out

def _to_metadata_string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _render_skill_markdown(opencode_name: str, entry: SkillIndexEntry, frontmatter: dict, body: str) -> str:
    efp_extra = {k: v for k, v in frontmatter.items() if k not in KNOWN_FIELDS}
    metadata_raw: dict[str, Any] = {
        "efp_name": entry.efp_name,
        "efp_version": str(frontmatter.get("version", "")),
        "efp_owner": frontmatter.get("owner", ""),
        "efp_tools": ",".join(entry.tools),
        "efp_task_tools": ",".join(entry.task_tools),
        "efp_risk_level": entry.risk_level or "unknown",
        "efp_source_path": entry.source_path,
        "efp_triggers": frontmatter.get("triggers", []),
        "efp_when_to_use": frontmatter.get("when_to_use", []),
        "efp_extra": efp_extra,
    }
    for optional in ("output_format", "model", "hooks"):
        if optional in frontmatter:
            metadata_raw[f"efp_{optional}"] = frontmatter.get(optional)

    metadata = {k: _to_metadata_string(v) for k, v in metadata_raw.items()}

    header = {
        "name": opencode_name,
        "description": entry.description,
        "license": "internal",
        "compatibility": "opencode",
        "metadata": metadata,
    }
    fm = yaml.safe_dump(header, sort_keys=False, allow_unicode=True).strip()
    title = entry.description or entry.efp_name
    return (
        f"---\n{fm}\n---\n\n"
        f"# {title}\n\n"
        f"{GENERATED_MARKER}\n"
        f"Original EFP skill name: `{entry.efp_name}`\n"
        f"Original source path: `{entry.source_path}`\n\n"
        "## OpenCode Compatibility\n"
        f"- Compatibility: {entry.opencode_compatibility}\n"
        f"- Runtime equivalence: {'full' if entry.runtime_equivalence else ('unsupported' if not entry.opencode_supported else 'partial')}\n"
        f"- Programmatic skill.py detected: {'yes' if entry.programmatic else 'no'}\n"
        f"- OpenCode supported: {'yes' if entry.opencode_supported else 'no'}\n"
        "Important: the OpenCode adapter does not execute EFP skill.py directly. Generated skills are prompt assets unless an explicit OpenCode wrapper/tool is declared.\n\n"
        + ("This skill is not supported for OpenCode runtime. Do not execute it as if it were native EFP.\n\n" if not entry.opencode_supported else "")
        + ("Runtime equivalence is partial. Explain this limitation to the user when it affects execution.\n\n" if not entry.runtime_equivalence else "")
        + ("## Compatibility Warnings\n" + "\n".join([f"- {w}" for w in entry.compatibility_warnings]) + "\n\n" if entry.compatibility_warnings else "")
        + ("## Available OpenCode Tools\n" + ("\n".join([f"- {(m['efp_name'] or '[declared]')} -> {m['opencode_name']}" for m in entry.tool_mappings if m.get("available")]) or "No mapped OpenCode tools were found.") + "\n\n")
        + ("## Missing Required Tools\n" + "\n".join([f"- {_mapping_display_name(m)}: {m.get('missing_reason')}" for m in entry.tool_mappings if not m.get("available")]) + "\n\n" if any(not m.get("available") for m in entry.tool_mappings) else "")
        + f"{body.rstrip()}\n\n"
        "## OpenCode Runtime Notes\n"
        "- Use only the mapped OpenCode tool names listed above.\n"
        "- Do not call external writeback tools unless Portal policy allows it.\n"
        "- If required tools are unavailable, explain the blocker instead of using raw curl.\n"
    )


def _write_subagent_prompt(agents_dir: Path, entry: SkillIndexEntry) -> None:
    agents_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = agents_dir / f"skill-{entry.opencode_name}.md"
    tools = ", ".join(entry.tools) if entry.tools else "none"
    task_tools = ", ".join(entry.task_tools) if entry.task_tools else "none"
    runtime = "full" if entry.runtime_equivalence else ("unsupported" if not entry.opencode_supported else "partial")
    op_tools = ", ".join(entry.opencode_tools) if entry.opencode_tools else "none"
    missing = ", ".join(entry.missing_tools) if entry.missing_tools else "none"
    missing_declared = ", ".join(entry.missing_opencode_tools) if entry.missing_opencode_tools else "none"
    warnings_text = "; ".join(entry.compatibility_warnings) if entry.compatibility_warnings else "none"
    content = f"""---
name: skill-{entry.opencode_name}
description: Run the generated OpenCode skill {entry.opencode_name} from EFP skill {entry.efp_name}.
---

You are an OpenCode subagent prompt generated for an EFP skill.

Skill:
- OpenCode skill name: {entry.opencode_name}
- Original EFP skill name: {entry.efp_name}
- Description: {entry.description}
- Required EFP tools: {tools}
- Required EFP task tools: {task_tools}
- Compatibility: {entry.opencode_compatibility}
- Runtime equivalence: {runtime}
- Programmatic: {'yes' if entry.programmatic else 'no'}
- OpenCode supported: {'yes' if entry.opencode_supported else 'no'}
- Compatibility warnings: {warnings_text}
- Mapped OpenCode tools: {op_tools}
- Missing required tools: {missing}
- Missing declared OpenCode tools: {missing_declared}

Instructions:
- Follow the generated skill at .opencode/skills/{entry.opencode_name}/SKILL.md.
- If OpenCode supported is no, report this skill as unsupported and do not execute it as native EFP.
- If runtime equivalence is partial, explain the limitation when it affects execution.
- Use mapped OpenCode tool names only.
- Do not call external writeback tools unless Portal policy allows it.
- If a required tool is missing, report blocker instead of inventing raw curl/bash/API calls.
- Declared OpenCode tools are usable only when present and enabled in tools-index.
"""
    prompt_path.write_text(content, encoding="utf-8")


def _render_command_markdown(entry: SkillIndexEntry) -> str:
    return f"""---
description: Run EFP skill {entry.opencode_name}
agent: efp-main
---

{GENERATED_COMMAND_MARKER}

Run the OpenCode agent skill `{entry.opencode_name}`.

Arguments:
$ARGUMENTS

Instructions:
1. Use the native OpenCode `skill` tool to load `{entry.opencode_name}`.
2. Follow `.opencode/skills/{entry.opencode_name}/SKILL.md`.
3. Do not claim success until the skill has been loaded and applied.
4. If the skill cannot be loaded, or required tools are unavailable, report the blocker.
"""


def sync_skills(skills_dir: Path, opencode_skills_dir: Path, state_dir: Path, tools_index: dict[str, Any] | None = None, opencode_commands_dir: Path | None = None) -> SkillsIndex:
    opencode_skills_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    if opencode_commands_dir is None:
        opencode_commands_dir = opencode_skills_dir.parent / "commands"
    opencode_commands_dir.mkdir(parents=True, exist_ok=True)

    warnings_list: list[str] = []
    skills: list[SkillIndexEntry] = []
    if tools_index is None:
        tip = state_dir / "tools-index.json"
        if tip.exists():
            try:
                tools_index = json.loads(tip.read_text(encoding="utf-8"))
            except Exception:
                tools_index = {"tools": []}
    tool_map = build_tool_name_map(tools_index or {"tools": []})

    if not skills_dir.exists():
        msg = f"skills directory does not exist: {skills_dir}"
        warnings.warn(msg)
        warnings_list.append(msg)
        index = SkillsIndex(
            generated_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            skills=[],
            warnings=warnings_list,
        )
        index_path = state_dir / "skills-index.json"
        with index_path.open("w", encoding="utf-8") as f:
            json.dump(index.to_json_dict(), f, ensure_ascii=False, indent=2)
            f.write("\n")
        return index

    discovered = _discover_skill_files(skills_dir)
    current_opencode_names: set[str] = set()
    for source_path in discovered:
        content = source_path.read_text(encoding="utf-8")
        parsed = _split_frontmatter(content)
        if parsed is None:
            continue
        frontmatter, _ = parsed
        raw_efp_name = str(frontmatter.get("name") or (source_path.parent.name if source_path.name == "skill.md" else source_path.stem))
        current_opencode_names.add(normalize_skill_name(raw_efp_name, str(source_path.resolve())))

    for child in opencode_skills_dir.iterdir():
        if not child.is_dir() or child.name in current_opencode_names:
            continue
        candidate = child / "SKILL.md"
        if not candidate.exists():
            continue
        if GENERATED_MARKER in candidate.read_text(encoding="utf-8"):
            shutil.rmtree(child)

    for cmd_file in opencode_commands_dir.glob("*.md"):
        if cmd_file.read_text(encoding="utf-8").find(GENERATED_COMMAND_MARKER) >= 0 and cmd_file.stem not in current_opencode_names:
            cmd_file.unlink()

    name_to_source: dict[str, Path] = {}
    generated_commands: set[str] = set()
    for source_path in discovered:
        content = source_path.read_text(encoding="utf-8")
        parsed = _split_frontmatter(content)
        if parsed is None:
            continue
        frontmatter, body = parsed

        raw_efp_name = str(frontmatter.get("name") or (source_path.parent.name if source_path.name == "skill.md" else source_path.stem))
        opencode_name = normalize_skill_name(raw_efp_name, str(source_path.resolve()))
        if opencode_name in name_to_source:
            first_source = name_to_source[opencode_name]
            raise ValueError(
                f"duplicate normalized skill name: normalized name={opencode_name}, "
                f"source_path={first_source}, source_path={source_path}"
            )
        name_to_source[opencode_name] = source_path

        description = (frontmatter.get("description") or "").strip() if isinstance(frontmatter.get("description", ""), str) else ""
        if not description:
            description = f"EFP skill {raw_efp_name}"
            msg = f"{source_path}: missing description, fallback to '{description}'"
            warnings.warn(msg)
            warnings_list.append(msg)

        tools = _validate_list_field(frontmatter, source_path, "tools")
        task_tools = _validate_list_field(frontmatter, source_path, "task_tools")
        risk_level = frontmatter.get("risk_level") or "unknown"

        target_path = opencode_skills_dir / opencode_name / "SKILL.md"
        target_path.parent.mkdir(parents=True, exist_ok=True)

        programmatic = _is_programmatic_skill(source_path, frontmatter)
        supported = _opencode_supported(frontmatter)
        has_wrapper = _has_explicit_opencode_wrapper(frontmatter)
        compat_warnings = []
        if not supported:
            op_compat = "unsupported"; runtime_eq = False; compat_warnings.append("skill is marked unsupported for OpenCode runtime")
        elif programmatic and not has_wrapper:
            op_compat = "programmatic_prompt_only"; runtime_eq = False; compat_warnings.append("EFP skill.py is not executed by the OpenCode adapter; generated skill is prompt-only")
        elif programmatic and has_wrapper:
            op_compat = "programmatic_wrapper"; runtime_eq = _declared_runtime_equivalence(frontmatter) == "full"
            if not runtime_eq: compat_warnings.append("programmatic OpenCode wrapper declared but runtime equivalence is not marked full")
        else:
            op_compat = "prompt_only"; runtime_eq = True
        tool_mappings=[]; op_tools=[]; miss=[]; missing_op_tools=[]
        explicit_map = _frontmatter_tool_mapping(frontmatter)
        explicit_op_tools = _frontmatter_opencode_tools(frontmatter)
        seen_tools = []
        for x in list(tools)+list(task_tools):
            if x not in seen_tools:
                seen_tools.append(x)
        for efp_tool in seen_tools:
            explicit_op = explicit_map.get(efp_tool)
            meta = tool_map.get(explicit_op) if explicit_op else tool_map.get(efp_tool)
            enabled = bool(meta.get("enabled", True)) if isinstance(meta, dict) else False
            if meta and meta.get("opencode_name") and enabled:
                m={"efp_name":efp_tool,"opencode_name":meta.get("opencode_name"),"available":True,"capability_id":meta.get("capability_id"),"policy_tags":meta.get("policy_tags",[]),"risk_level":meta.get("risk_level"),"requires_identity_binding":bool(meta.get("requires_identity_binding")),"source_ref":meta.get("source_ref"),"enabled":enabled,"mapping_source":("frontmatter+tools-index" if explicit_op else "tools-index")}
                tool_mappings.append(m)
                if meta.get("opencode_name") not in op_tools: op_tools.append(meta.get("opencode_name"))
            else:
                if explicit_op:
                    reason = "declared OpenCode wrapper is disabled" if meta and meta.get("opencode_name") and not enabled else "declared OpenCode wrapper is not present in tools-index"
                    miss_name = efp_tool
                    missing_op_name = explicit_op
                else:
                    reason = "matching OpenCode wrapper is disabled" if meta and meta.get("opencode_name") and not enabled else "no matching OpenCode wrapper in tools-index"
                    miss_name = efp_tool
                    missing_op_name = meta.get("opencode_name") if isinstance(meta, dict) else None
                tool_mappings.append({"efp_name":miss_name,"opencode_name":missing_op_name,"available":False,"enabled":(False if isinstance(meta, dict) else None),"missing_reason":reason,"mapping_source":("frontmatter+tools-index" if explicit_op else "tools-index")})
                if efp_tool not in miss: miss.append(efp_tool)
        for op_tool in explicit_op_tools:
            if op_tool in op_tools:
                continue
            meta = tool_map.get(op_tool)
            enabled = bool(meta.get("enabled", True)) if isinstance(meta, dict) else False
            if meta and meta.get("opencode_name") and enabled:
                op_tools.append(meta.get("opencode_name"))
                tool_mappings.append({"efp_name":"","opencode_name":meta.get("opencode_name"),"available":True,"capability_id":meta.get("capability_id"),"policy_tags":meta.get("policy_tags",[]),"risk_level":meta.get("risk_level"),"requires_identity_binding":bool(meta.get("requires_identity_binding")),"source_ref":meta.get("source_ref"),"enabled":enabled,"mapping_source":"frontmatter_opencode_tools"})
            else:
                compat_warnings.append(f"declared OpenCode tool {op_tool} is not present/enabled in tools-index")
                missing_op_tools.append(op_tool)
                reason = "declared OpenCode tool is disabled" if meta and meta.get("opencode_name") and not enabled else "declared OpenCode tool is not present in tools-index"
                tool_mappings.append({"efp_name":"","opencode_name":op_tool,"available":False,"enabled":(False if isinstance(meta, dict) else None),"missing_reason":reason,"mapping_source":"frontmatter_opencode_tools"})

        entry = SkillIndexEntry(
            efp_name=raw_efp_name,
            opencode_name=opencode_name,
            description=description,
            tools=tools,
            task_tools=task_tools,
            risk_level=risk_level,
            source_path=str(source_path.resolve()),
            target_path=str(target_path.resolve()),
            opencode_compatibility=op_compat, runtime_equivalence=runtime_eq, programmatic=programmatic, opencode_supported=supported, compatibility_warnings=compat_warnings, tool_mappings=tool_mappings, opencode_tools=op_tools, missing_tools=miss, missing_opencode_tools=missing_op_tools,
        )
        target_path.write_text(_render_skill_markdown(opencode_name, entry, frontmatter, body), encoding="utf-8")
        skills.append(entry)
        if entry.opencode_supported and entry.runtime_equivalence and (not entry.missing_tools) and (not entry.missing_opencode_tools):
            (opencode_commands_dir / f"{entry.opencode_name}.md").write_text(_render_command_markdown(entry), encoding="utf-8")
            generated_commands.add(entry.opencode_name)

        if task_tools or raw_efp_name in SUBAGENT_ALLOWLIST or opencode_name in SUBAGENT_ALLOWLIST:
            _write_subagent_prompt(opencode_skills_dir.parent / "agents", entry)

    for cmd_file in opencode_commands_dir.glob("*.md"):
        if cmd_file.stem in generated_commands:
            continue
        text = cmd_file.read_text(encoding="utf-8")
        if GENERATED_COMMAND_MARKER in text:
            cmd_file.unlink()

    index = SkillsIndex(
        generated_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        skills=skills,
        warnings=warnings_list,
    )

    index_path = state_dir / "skills-index.json"
    with index_path.open("w", encoding="utf-8") as f:
        json.dump(index.to_json_dict(), f, ensure_ascii=False, indent=2)
        f.write("\n")
    return index


def main() -> None:
    settings = Settings.from_env()
    parser = argparse.ArgumentParser(description="Sync EFP skills into OpenCode skills")
    parser.add_argument("--skills-dir", type=Path, default=settings.skills_dir)
    parser.add_argument(
        "--opencode-skills-dir",
        type=Path,
        default=settings.workspace_dir / ".opencode" / "skills",
    )
    parser.add_argument("--state-dir", type=Path, default=settings.adapter_state_dir)
    parser.add_argument("--tools-index", type=Path, default=None)
    args = parser.parse_args()

    tools_index=None
    if args.tools_index and args.tools_index.exists():
        tools_index=json.loads(args.tools_index.read_text(encoding="utf-8"))
    index = sync_skills(args.skills_dir, args.opencode_skills_dir, args.state_dir, tools_index=tools_index)
    print(f"synced {len(index.skills)} skills")
    print(f"wrote {args.state_dir / 'skills-index.json'}")
    for warning_text in index.warnings:
        print(f"warning: {warning_text}")


if __name__ == "__main__":
    main()
