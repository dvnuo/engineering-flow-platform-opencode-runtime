from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .settings import Settings


@dataclass(frozen=True)
class CompatStatePaths:
    root: Path
    tasks_dir: Path
    sessions_dir: Path
    attachments_dir: Path
    usage_dir: Path
    chatlogs_dir: Path
    usage_file: Path
    portal_metadata_pending_file: Path


def ensure_state_dirs(settings: Settings) -> CompatStatePaths:
    root = settings.adapter_state_dir
    paths = CompatStatePaths(
        root=root,
        tasks_dir=root / "tasks",
        sessions_dir=root / "sessions",
        attachments_dir=root / "attachments",
        usage_dir=root / "usage",
        chatlogs_dir=root / "chatlogs",
        usage_file=root / "usage.jsonl",
        portal_metadata_pending_file=root / "portal_metadata_pending.jsonl",
    )
    for p in [paths.root, paths.tasks_dir, paths.sessions_dir, paths.attachments_dir, paths.usage_dir, paths.chatlogs_dir]:
        p.mkdir(parents=True, exist_ok=True)
    return paths


def probe_writable(path: Path) -> dict:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe_file = path / f".efp-write-probe-{uuid4().hex}.tmp"
        probe_file.write_text("ok", encoding="utf-8")
        probe_file.unlink(missing_ok=True)
        return {"path": str(path), "exists": path.exists(), "writable": True}
    except Exception as exc:
        return {"path": str(path), "exists": path.exists(), "writable": False, "error": str(exc).split("\n", 1)[0][:200]}


def build_state_health_snapshot(settings: Settings, state_paths: CompatStatePaths) -> dict:
    paths = {
        "adapter_state_dir": probe_writable(state_paths.root),
        "tasks_dir": probe_writable(state_paths.tasks_dir),
        "sessions_dir": probe_writable(state_paths.sessions_dir),
        "chatlogs_dir": probe_writable(state_paths.chatlogs_dir),
        "opencode_data_dir": probe_writable(settings.opencode_data_dir),
        "workspace_dir": probe_writable(settings.workspace_dir),
    }
    return {"healthy": all(bool(item.get("writable")) for item in paths.values()), "paths": paths}
