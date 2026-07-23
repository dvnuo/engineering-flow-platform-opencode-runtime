from __future__ import annotations

import json
from datetime import datetime, timezone

from .agents_md import ensure_default_agents_md
from .index_loader import read_json_file, load_skills_index
from .opencode_config import build_opencode_config, merge_with_existing_config, write_opencode_config
from .runtime_env import ensure_opencode_xdg_data_home
from .settings import Settings
from .skill_sync import sync_runtime_skills
from .workspace_gitignore import ensure_workspace_gitignore



def init_assets(settings: Settings) -> None:
    required_dirs = [
        settings.workspace_dir,
        settings.workspace_dir / ".opencode",
        settings.workspace_dir / ".opencode" / "skills",
        settings.workspace_dir / ".opencode" / "agents",
        settings.workspace_dir / ".opencode" / "commands",
        settings.skills_dir,
        settings.opencode_data_dir,
        settings.adapter_state_dir,
    ]
    for d in required_dirs:
        d.mkdir(parents=True, exist_ok=True)
    ensure_opencode_xdg_data_home(settings)
    sync_runtime_skills(settings)

    ensure_default_agents_md(settings)
    ensure_workspace_gitignore(settings)
    _refresh_managed_opencode_config(settings)


def _refresh_managed_opencode_config(settings: Settings) -> None:
    config_path = settings.opencode_config_path
    generated, _, _ = build_opencode_config(settings, runtime_config=None)
    existing = read_json_file(config_path) if config_path.exists() else None
    if config_path.exists() and existing is None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        backup = config_path.with_name(f"{config_path.name}.bak.{ts}")
        backup.write_text(config_path.read_text(encoding="utf-8"), encoding="utf-8")
    merged = merge_with_existing_config(
        existing,
        generated,
        skills_index=load_skills_index(settings),
    )
    write_opencode_config(settings, merged)
    print(f"Updated managed config in {config_path}")


def main() -> None:
    init_assets(Settings.from_env())


if __name__ == "__main__":
    main()
