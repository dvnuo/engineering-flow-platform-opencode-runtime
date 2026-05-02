from __future__ import annotations

import argparse
import hashlib
import json
import re
import warnings
from dataclasses import asdict, dataclass, field
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
}

SUBAGENT_ALLOWLIST = {"review-pull-request", "create-pull-request"}
SKIP_DIRS = {".git", ".github", "scripts", "__pycache__"}


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


@dataclass(frozen=True)
class SkillsIndex:
    generated_at: str
    skills: list[SkillIndexEntry]
    warnings: list[str] = field(default_factory=list)

    def to_json_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "skills": [asdict(x) for x in self.skills],
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


def _render_skill_markdown(opencode_name: str, entry: SkillIndexEntry, frontmatter: dict, body: str) -> str:
    efp_extra = {k: v for k, v in frontmatter.items() if k not in KNOWN_FIELDS}
    metadata: dict = {
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
            metadata[f"efp_{optional}"] = frontmatter.get(optional)

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
        "This skill was generated from an EFP skill asset.\n"
        f"Original EFP skill name: `{entry.efp_name}`\n"
        f"Original source path: `{entry.source_path}`\n\n"
        f"{body.rstrip()}\n\n"
        "## OpenCode Runtime Notes\n"
        "- Use efp_* tools when available.\n"
        "- Do not call external writeback tools unless Portal policy allows it.\n"
        "- If required tools are unavailable, explain the blocker instead of using raw curl.\n"
    )


def _write_subagent_prompt(agents_dir: Path, entry: SkillIndexEntry) -> None:
    agents_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = agents_dir / f"skill-{entry.opencode_name}.md"
    tools = ", ".join(entry.tools) if entry.tools else "none"
    task_tools = ", ".join(entry.task_tools) if entry.task_tools else "none"
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

Instructions:
- Follow the generated skill at .opencode/skills/{entry.opencode_name}/SKILL.md.
- Use efp_* tools when available.
- Do not call external writeback tools unless Portal policy allows it.
- If required tools are unavailable, report the blocker instead of using raw curl or ad hoc credentials.
"""
    prompt_path.write_text(content, encoding="utf-8")


def sync_skills(skills_dir: Path, opencode_skills_dir: Path, state_dir: Path) -> SkillsIndex:
    opencode_skills_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    warnings_list: list[str] = []
    skills: list[SkillIndexEntry] = []

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

    name_to_source: dict[str, Path] = {}
    for source_path in _discover_skill_files(skills_dir):
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

        entry = SkillIndexEntry(
            efp_name=raw_efp_name,
            opencode_name=opencode_name,
            description=description,
            tools=tools,
            task_tools=task_tools,
            risk_level=risk_level,
            source_path=str(source_path.resolve()),
            target_path=str(target_path.resolve()),
        )
        target_path.write_text(_render_skill_markdown(opencode_name, entry, frontmatter, body), encoding="utf-8")
        skills.append(entry)

        if task_tools or raw_efp_name in SUBAGENT_ALLOWLIST or opencode_name in SUBAGENT_ALLOWLIST:
            _write_subagent_prompt(opencode_skills_dir.parent / "agents", entry)

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
    args = parser.parse_args()

    index = sync_skills(args.skills_dir, args.opencode_skills_dir, args.state_dir)
    print(f"synced {len(index.skills)} skills")
    print(f"wrote {args.state_dir / 'skills-index.json'}")
    for warning_text in index.warnings:
        print(f"warning: {warning_text}")


if __name__ == "__main__":
    main()
