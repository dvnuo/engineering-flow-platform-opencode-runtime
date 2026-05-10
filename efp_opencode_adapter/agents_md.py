from __future__ import annotations

from pathlib import Path

from .settings import Settings

AGENTS_MD_FILENAME = "AGENTS.md"

DEFAULT_AGENTS_MD = """# AGENTS.md
This OpenCode runtime is managed by EFP Portal.

## Runtime rules
- Treat this file as the project-level instruction source for OpenCode.
- Obey Portal capability, runtime profile, and policy metadata.
- Do not write back to external systems unless explicitly allowed by policy.
- Prefer EFP-provided tools such as efp_* for GitHub, Jira, Confluence, and other managed integrations when available.
"""


def agents_md_path(settings: Settings) -> Path:
    return settings.workspace_dir / AGENTS_MD_FILENAME


def legacy_agents_prompt_path(settings: Settings) -> Path:
    return settings.adapter_state_dir / "system_prompts" / "agents.md"


def legacy_efp_main_prompt_path(settings: Settings) -> Path:
    return settings.workspace_dir / ".opencode" / "agents" / "efp-main.md"


def _write_agents_md(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(path)
    return path


def ensure_default_agents_md(settings: Settings) -> Path:
    settings.workspace_dir.mkdir(parents=True, exist_ok=True)
    path = agents_md_path(settings)
    if path.exists():
        return path

    content = DEFAULT_AGENTS_MD
    for legacy_path in (legacy_agents_prompt_path(settings), legacy_efp_main_prompt_path(settings)):
        if not legacy_path.exists():
            continue
        try:
            content = legacy_path.read_text(encoding="utf-8")
            break
        except Exception:
            content = DEFAULT_AGENTS_MD

    return _write_agents_md(path, content)


def read_agents_md(settings: Settings) -> str:
    path = ensure_default_agents_md(settings)
    return path.read_text(encoding="utf-8")


def write_agents_md(settings: Settings, content: str) -> Path:
    path = agents_md_path(settings)
    return _write_agents_md(path, content)
