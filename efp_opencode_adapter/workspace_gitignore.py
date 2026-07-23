from __future__ import annotations

from pathlib import Path

from .settings import Settings

GITIGNORE_FILENAME = ".gitignore"

# OpenCode takes a git shadow snapshot of the workspace before it runs. On the
# EFS-backed PVC, hashing dependency/build trees (node_modules, target, .venv,
# ...) dominates that snapshot and therefore the request latency, so the runtime
# provisions a .gitignore on first boot when the workspace has none.
DEFAULT_GITIGNORE_ENTRIES = (
    "node_modules/",
    ".pnpm-store/",
    ".yarn/",
    "bower_components/",
    "target/",
    "build/",
    "dist/",
    "out/",
    ".venv/",
    "venv/",
    "__pycache__/",
    ".m2/",
    ".gradle/",
    ".next/",
    ".nuxt/",
    ".cache/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    "*.pyc",
    "*.class",
    "*.jar",
    ".DS_Store",
)

DEFAULT_GITIGNORE_HEADER = (
    "# Provisioned by the EFP OpenCode runtime because the workspace had no .gitignore.",
    "# OpenCode snapshots the workspace before every run; keeping dependency and",
    "# build trees out of that snapshot is what keeps a request fast on a network PVC.",
    "# This file is yours to edit: the runtime never overwrites an existing .gitignore.",
    "",
)


def workspace_gitignore_path(settings: Settings) -> Path:
    return settings.workspace_dir / GITIGNORE_FILENAME


def default_gitignore_content() -> str:
    return "\n".join((*DEFAULT_GITIGNORE_HEADER, *DEFAULT_GITIGNORE_ENTRIES)) + "\n"


def _write_gitignore(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(path)
    return path


def ensure_workspace_gitignore(settings: Settings) -> Path:
    """Create ``<workspace>/.gitignore`` only when absent; never touch a user's own file."""
    path = workspace_gitignore_path(settings)
    if path.exists():
        print(f"workspace.gitignore.kept path={path} reason=already_present")
        return path
    _write_gitignore(path, default_gitignore_content())
    print(f"workspace.gitignore.created path={path} entries={len(DEFAULT_GITIGNORE_ENTRIES)}")
    return path
