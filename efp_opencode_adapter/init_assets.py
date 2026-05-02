from __future__ import annotations

import json
from pathlib import Path

from .settings import Settings
from .skill_sync import sync_skills


def _minimal_config() -> dict:
    return {
        "$schema": "https://opencode.ai/config.json",
        "autoupdate": False,
        "share": "disabled",
        "server": {"hostname": "127.0.0.1", "port": 4096},
        "permission": {
            "*": "ask",
            "read": {"*": "allow", "*.env": "deny", "*.env.*": "deny", "*.env.example": "allow"},
            "glob": "allow",
            "grep": "allow",
            "edit": "ask",
            "bash": {
                "*": "ask",
                "git status*": "allow",
                "git diff*": "allow",
                "git log*": "allow",
                "rm *": "deny",
                "sudo *": "deny",
                "git push *": "deny",
                "curl *|*bash*": "deny",
            },
            "external_directory": "deny",
            "webfetch": "ask",
            "websearch": "ask",
        },
    }


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
        home / ".local" / "share" / "opencode",
        settings.adapter_state_dir,
    ]
    for d in required_dirs:
        d.mkdir(parents=True, exist_ok=True)

    sync_skills(
        settings.skills_dir,
        settings.workspace_dir / ".opencode" / "skills",
        settings.adapter_state_dir,
    )

    config_path = settings.opencode_config_path
    if config_path.exists():
        print(f"{config_path} exists, leaving unchanged")
        return
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w", encoding="utf-8") as f:
        json.dump(_minimal_config(), f, indent=2)
        f.write("\n")
    print(f"Created {config_path}")


def main() -> None:
    init_assets(Settings.from_env())


if __name__ == "__main__":
    main()
