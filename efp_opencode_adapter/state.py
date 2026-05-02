from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .settings import Settings


@dataclass(frozen=True)
class CompatStatePaths:
    root: Path
    tasks_dir: Path
    sessions_dir: Path
    attachments_dir: Path
    usage_dir: Path


def ensure_state_dirs(settings: Settings) -> CompatStatePaths:
    root = settings.adapter_state_dir
    paths = CompatStatePaths(
        root=root,
        tasks_dir=root / "tasks",
        sessions_dir=root / "sessions",
        attachments_dir=root / "attachments",
        usage_dir=root / "usage",
    )
    for p in [paths.root, paths.tasks_dir, paths.sessions_dir, paths.attachments_dir, paths.usage_dir]:
        p.mkdir(parents=True, exist_ok=True)
    return paths
