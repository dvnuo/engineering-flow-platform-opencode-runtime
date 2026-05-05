from __future__ import annotations

from pathlib import Path

from .opencode_config import build_opencode_config, write_main_agent_prompt, write_opencode_config
from .settings import Settings
from .skill_sync import sync_skills
from .tool_sync import sync_tools



def init_assets(settings: Settings) -> None:
    home = Path("/home/opencode")
    required_dirs = [
        settings.workspace_dir,
        settings.workspace_dir / ".opencode",
        settings.workspace_dir / ".opencode" / "skills",
        settings.workspace_dir / ".opencode" / "tools",
        settings.workspace_dir / ".opencode" / "agents",
        settings.skills_dir,
        settings.tools_dir,
        settings.opencode_data_dir,
        settings.adapter_state_dir,
    ]
    for d in required_dirs:
        d.mkdir(parents=True, exist_ok=True)

    sync_skills(
        settings.skills_dir,
        settings.workspace_dir / ".opencode" / "skills",
        settings.adapter_state_dir,
    )

    sync_tools(
        settings.tools_dir,
        settings.workspace_dir / ".opencode" / "tools",
        settings.adapter_state_dir,
    )

    write_main_agent_prompt(settings)
    config_path = settings.opencode_config_path
    if config_path.exists():
        print(f"{config_path} exists, leaving unchanged")
        return
    config, _, _ = build_opencode_config(settings, runtime_config=None)
    write_opencode_config(settings, config)
    print(f"Created {config_path}")


def main() -> None:
    init_assets(Settings.from_env())


if __name__ == "__main__":
    main()
