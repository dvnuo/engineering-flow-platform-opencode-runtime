from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import warnings
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
    "kind",
    "runtime",
    "execution",
    "runtime_compat",
    "opencode_supported",
    "opencode",
    "opencode_runtime_equivalence",
}
GENERATED_MARKER = "This skill was generated from an EFP skill asset."
GENERATED_COMMAND_MARKER = "This command was generated from an EFP skill asset."
SKILL_ENTRY_FILENAMES = ("SKILL.md", "skill.md")
SKILL_RESOURCE_EXCLUDE_DIRS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache"}
SKILL_RESOURCE_EXCLUDE_FILES = {"SKILL.md", "skill.md", ".DS_Store"}


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
    resource_paths: list[str] = field(default_factory=list)


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


def _warn(message: str, warnings_list: list[str] | None = None) -> None:
    warnings.warn(message, stacklevel=2)
    if warnings_list is not None:
        warnings_list.append(message)


def _split_frontmatter(text: str, source_path: Path | None = None):
    if not text.startswith("---\n"):
        return None
    i = text.find("\n---\n", 4)
    if i == -1:
        return None
    try:
        p = yaml.safe_load(text[4:i]) or {}
    except yaml.YAMLError as exc:
        where = f"{source_path}: " if source_path is not None else ""
        raise ValueError(
            f"{where}invalid skill frontmatter YAML: {exc}. "
            "Quote scalar values that contain ': ', or use a YAML block scalar."
        ) from exc
    if not isinstance(p, dict):
        where = f"{source_path}: " if source_path is not None else ""
        raise ValueError(f"{where}frontmatter must be a mapping")
    return p, text[i + 5 :]


def _read_skill_frontmatter(source_path: Path, warnings_list: list[str] | None = None):
    try:
        return _split_frontmatter(source_path.read_text(encoding="utf-8"), source_path=source_path)
    except ValueError as exc:
        _warn(f"skipping invalid skill asset: {exc}", warnings_list)
        return None


def _validate_list_field(frontmatter: dict, source_path: Path, field_name: str) -> list[str]:
    v = frontmatter.get(field_name, [])
    if v is None:
        return []
    if isinstance(v, list):
        if not all(isinstance(x, str) for x in v):
            raise ValueError(f"{source_path}: {field_name} must be list[str]")
        return v
    if v == "":
        return []
    raise ValueError(f"{source_path}: {field_name} must be list[str], got {type(v).__name__}")


def normalize_skill_name(raw_name: str, fallback_seed: str) -> str:
    n = re.sub(r"[^a-z0-9-]", "-", re.sub(r"\s+", "-", raw_name.lower().replace("_", "-"))).strip("-")
    return re.sub(r"-+", "-", n) or f"skill-{hashlib.sha256(fallback_seed.encode()).hexdigest()[:12]}"


def _is_directory_skill(source_path: Path, skills_dir: Path) -> bool:
    return source_path.parent != skills_dir and source_path.name in SKILL_ENTRY_FILENAMES


def _skill_fallback_name(source_path: Path, skills_dir: Path) -> str:
    if _is_directory_skill(source_path, skills_dir):
        return source_path.parent.name
    return source_path.stem


def _discover_skill_files(skills_dir: Path, warnings_list: list[str] | None = None) -> list[Path]:
    out: list[Path] = []

    for child in sorted(skills_dir.iterdir(), key=str):
        if not child.is_dir() or child.name.startswith("."):
            continue

        upper = child / "SKILL.md"
        lower = child / "skill.md"

        if upper.exists():
            out.append(upper)
            if lower.exists():
                _warn(f"{child}: both SKILL.md and skill.md exist; using SKILL.md", warnings_list)
        elif lower.exists():
            out.append(lower)

    for md in sorted(skills_dir.glob("*.md"), key=str):
        if _read_skill_frontmatter(md, warnings_list) is not None:
            out.append(md)

    return sorted(dict.fromkeys(out), key=str)


def _is_programmatic_skill(source_path: Path, fm: dict[str, Any]) -> bool:
    op = fm.get("opencode") if isinstance(fm.get("opencode"), dict) else {}
    return any(
        [
            (source_path.parent / "skill.py").exists(),
            source_path.with_suffix(".py").exists(),
            str(fm.get("kind", "")).lower() in {"programmatic", "hybrid"},
            str(fm.get("execution", "")).lower() == "python",
            str(fm.get("runtime", "")).lower() == "python",
            str(op.get("execution", "")).lower() == "python",
        ]
    )


def _opencode_supported(frontmatter: dict[str, Any]) -> bool:
    if frontmatter.get("opencode_supported") is False:
        return False
    rc = frontmatter.get("runtime_compat")
    if isinstance(rc, list) and not ({"opencode", "all"} & {str(x).lower() for x in rc}):
        return False
    if isinstance(rc, str) and rc.lower() not in {"opencode", "all"}:
        return False
    op = frontmatter.get("opencode") if isinstance(frontmatter.get("opencode"), dict) else {}
    return not (op.get("compatible") is False or op.get("supported") is False)


def _reset_managed_skill_dir(target_dir: Path) -> None:
    if not target_dir.exists():
        return

    marker = target_dir / "SKILL.md"
    if marker.exists():
        text = marker.read_text(encoding="utf-8", errors="ignore")
        if GENERATED_MARKER in text:
            shutil.rmtree(target_dir)
            return

    raise ValueError(f"target skill directory already exists and is not managed by EFP: {target_dir}")


def _copy_skill_resources(
    source_entry: Path,
    target_dir: Path,
    skills_dir: Path,
    warnings_list: list[str] | None = None,
) -> list[str]:
    copied: list[str] = []

    if not _is_directory_skill(source_entry, skills_dir):
        return copied

    source_dir = source_entry.parent
    for item in sorted(source_dir.rglob("*"), key=str):
        rel = item.relative_to(source_dir)

        if any(part in SKILL_RESOURCE_EXCLUDE_DIRS for part in rel.parts):
            continue

        if item.is_symlink():
            _warn(f"{item}: symlink skipped while syncing skill resources", warnings_list)
            continue

        if item.is_dir():
            continue

        if rel.name in SKILL_RESOURCE_EXCLUDE_FILES:
            continue

        dest = target_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, dest)
        copied.append(rel.as_posix())

    return copied


def _render_resource_instructions(opencode_name: str, copied_resources: list[str] | None) -> str:
    resources = sorted(copied_resources or [])
    if resources:
        shown = resources[:300]
        count_note = (
            f"Showing first 300 of {len(resources)} copied resource files.\n\n"
            if len(resources) > 300
            else ""
        )
        resource_list = count_note + "\n".join(f"* `{path}`" for path in shown)
    else:
        resource_list = "No sidecar resources were copied."

    return f"""## Synced skill package resources

This skill package is synced into `.opencode/skills/{opencode_name}/`.

Relative resource paths mentioned by this skill, such as `scripts/...`,
`templates/...`, `reference/...`, and `examples/...`, are relative to this
skill directory, not to the workspace root.

Before declaring a referenced resource missing, inspect the skill package:

```bash
find .opencode/skills/{opencode_name} -maxdepth 4 -type f | sort
```

To run a script resource, prefer:

```bash
cd .opencode/skills/{opencode_name} && python scripts/<script>.py --help
```

or use the explicit workspace-relative path:

```bash
python .opencode/skills/{opencode_name}/scripts/<script>.py --help
```

Copied resource files include:

{resource_list}
"""


def _render_skill_markdown(
    opencode_name: str,
    entry: SkillIndexEntry,
    frontmatter: dict,
    body: str,
    copied_resources: list[str] | None = None,
) -> str:
    meta = {
        "efp_name": entry.efp_name,
        "efp_tools": ",".join(entry.tools),
        "efp_task_tools": ",".join(entry.task_tools),
        "efp_source_path": entry.source_path,
    }
    fm = yaml.safe_dump(
        {
            "name": opencode_name,
            "description": entry.description,
            "license": "internal",
            "compatibility": "opencode",
            "metadata": meta,
        },
        sort_keys=False,
        allow_unicode=True,
    ).strip()
    warn = (
        "## Compatibility Warnings\n"
        + "\n".join([f"- {w}" for w in entry.compatibility_warnings])
        + "\n\n"
        if entry.compatibility_warnings
        else ""
    )
    resource_instructions = _render_resource_instructions(opencode_name, copied_resources)
    return (
        f"---\n{fm}\n---\n\n"
        f"# {entry.description or entry.efp_name}\n\n"
        f"{GENERATED_MARKER}\n\n"
        "This generated skill is a prompt asset.\n\n"
        "Source skill tools/task_tools metadata is informational only. Runtime tool access is controlled by "
        "OpenCode built-in tools, OpenCode MCP tools when enabled by OpenCode itself, skills, runtime profile, "
        "and permission policy.\n\n"
        f"{warn}{resource_instructions}\n\n{body.rstrip()}\n"
    )


def _render_command_markdown(entry: SkillIndexEntry) -> str:
    return (
        f"---\ndescription: Run EFP skill {entry.opencode_name}\nagent: efp-main\n---\n\n"
        f"{GENERATED_COMMAND_MARKER}\n\nRun the OpenCode agent skill `{entry.opencode_name}`.\n"
    )


def sync_skills(
    skills_dir: Path,
    opencode_skills_dir: Path,
    state_dir: Path,
    opencode_commands_dir: Path | None = None,
) -> SkillsIndex:
    opencode_skills_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    if opencode_commands_dir is None:
        opencode_commands_dir = opencode_skills_dir.parent / "commands"
    opencode_commands_dir.mkdir(parents=True, exist_ok=True)

    warnings_list: list[str] = []
    skills: list[SkillIndexEntry] = []
    if not skills_dir.exists():
        msg = f"skills directory does not exist: {skills_dir}"
        _warn(msg, warnings_list)
        idx = SkillsIndex(datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), [], warnings_list)
        (state_dir / "skills-index.json").write_text(
            json.dumps(idx.to_json_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return idx

    discovered = _discover_skill_files(skills_dir, warnings_list)
    parsed: dict[Path, tuple[dict[str, Any], str]] = {}
    current: set[str] = set()
    for sp in discovered:
        p = _read_skill_frontmatter(sp, warnings_list)
        if p:
            parsed[sp] = p
            raw = str(p[0].get("name") or _skill_fallback_name(sp, skills_dir))
            current.add(normalize_skill_name(raw, str(sp.resolve())))

    for child in opencode_skills_dir.iterdir():
        marker = child / "SKILL.md"
        if (
            child.is_dir()
            and child.name not in current
            and marker.exists()
            and GENERATED_MARKER in marker.read_text(encoding="utf-8", errors="ignore")
        ):
            shutil.rmtree(child)

    generated: set[str] = set()
    name_to_source: dict[str, Path] = {}
    for sp, p in parsed.items():
        fm, body = p
        raw = str(fm.get("name") or _skill_fallback_name(sp, skills_dir))
        name = normalize_skill_name(raw, str(sp.resolve()))
        if name in name_to_source:
            raise ValueError(
                f"duplicate normalized skill name: normalized name={name}, "
                f"source_path={name_to_source[name]}, source_path={sp}"
            )
        name_to_source[name] = sp

        desc = (fm.get("description") or "").strip() if isinstance(fm.get("description", ""), str) else ""
        if not desc:
            desc = f"EFP skill {raw}"
            warnings_list.append(f"{sp}: missing description, fallback to '{desc}'")
        tools = _validate_list_field(fm, sp, "tools")
        task = _validate_list_field(fm, sp, "task_tools")
        risk = fm.get("risk_level") or "unknown"
        target_dir = opencode_skills_dir / name
        t = target_dir / "SKILL.md"
        _reset_managed_skill_dir(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        copied_resources = _copy_skill_resources(sp, target_dir, skills_dir, warnings_list)

        programmatic = _is_programmatic_skill(sp, fm)
        supported = _opencode_supported(fm)
        compat: list[str] = []
        op = "prompt_only"
        eq = True
        if not supported:
            op = "unsupported"
            eq = False
            compat.append("skill is marked unsupported for OpenCode runtime")
        elif programmatic:
            op = "programmatic_prompt_only"
            eq = False
            compat.append("EFP skill.py is not executed by the OpenCode adapter; generated skill is prompt-only")

        e = SkillIndexEntry(
            efp_name=raw,
            opencode_name=name,
            description=desc,
            tools=tools,
            task_tools=task,
            risk_level=risk,
            source_path=str(sp.resolve()),
            target_path=str(t.resolve()),
            opencode_compatibility=op,
            runtime_equivalence=eq,
            programmatic=programmatic,
            opencode_supported=supported,
            compatibility_warnings=compat,
            resource_paths=copied_resources,
        )
        t.write_text(
            _render_skill_markdown(name, e, fm, body, copied_resources=copied_resources),
            encoding="utf-8",
        )
        skills.append(e)
        if e.opencode_supported and e.runtime_equivalence:
            (opencode_commands_dir / f"{e.opencode_name}.md").write_text(
                _render_command_markdown(e),
                encoding="utf-8",
            )
            generated.add(e.opencode_name)

    for cmd in opencode_commands_dir.glob("*.md"):
        if cmd.stem not in generated and GENERATED_COMMAND_MARKER in cmd.read_text(encoding="utf-8"):
            cmd.unlink()
    idx = SkillsIndex(datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), skills, warnings_list)
    (state_dir / "skills-index.json").write_text(
        json.dumps(idx.to_json_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return idx


def sync_runtime_skills(settings: Settings) -> SkillsIndex:
    return sync_skills(
        settings.skills_dir,
        settings.workspace_dir / ".opencode" / "skills",
        settings.adapter_state_dir,
        opencode_commands_dir=settings.workspace_dir / ".opencode" / "commands",
    )


def main() -> None:
    s = Settings.from_env()
    p = argparse.ArgumentParser(description="Sync EFP skills into OpenCode skills")
    p.add_argument("--skills-dir", type=Path, default=s.skills_dir)
    p.add_argument("--opencode-skills-dir", type=Path, default=s.workspace_dir / ".opencode" / "skills")
    p.add_argument("--state-dir", type=Path, default=s.adapter_state_dir)
    a = p.parse_args()
    idx = sync_skills(a.skills_dir, a.opencode_skills_dir, a.state_dir)
    print(f"synced {len(idx.skills)} skills")


if __name__ == "__main__":
    main()
