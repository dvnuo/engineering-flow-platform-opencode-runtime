from __future__ import annotations

from pathlib import Path

from .settings import Settings

AGENTS_MD_FILENAME = "AGENTS.md"
AGENTS_MD_EXAMPLE_FILENAME = "AGENTS.md.example"


def agents_md_path(settings: Settings) -> Path:
    return settings.workspace_dir / AGENTS_MD_FILENAME


def legacy_agents_prompt_path(settings: Settings) -> Path:
    return settings.adapter_state_dir / "system_prompts" / "agents.md"


def example_agents_md_path() -> Path:
    return Path(__file__).resolve().parents[1] / "workspace" / AGENTS_MD_EXAMPLE_FILENAME


def read_default_agents_md_template() -> str:
    path = example_agents_md_path()
    try:
        return path.read_text(encoding="utf-8")
    except Exception as exc:
        raise RuntimeError(f"Default AGENTS.md template not found or unreadable: {path}") from exc


def _write_agents_md(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(path)
    return path


def _read_legacy_agents_prompt(settings: Settings) -> str | None:
    path = legacy_agents_prompt_path(settings)
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return None


def ensure_default_agents_md(settings: Settings) -> Path:
    settings.workspace_dir.mkdir(parents=True, exist_ok=True)
    path = agents_md_path(settings)
    if path.exists():
        return path

    content = _read_legacy_agents_prompt(settings)
    if content is None:
        content = read_default_agents_md_template()

    return _write_agents_md(path, content)


def read_agents_md(settings: Settings) -> str:
    path = ensure_default_agents_md(settings)
    return path.read_text(encoding="utf-8")


def write_agents_md(settings: Settings, content: str) -> Path:
    path = agents_md_path(settings)
    return _write_agents_md(path, content)
