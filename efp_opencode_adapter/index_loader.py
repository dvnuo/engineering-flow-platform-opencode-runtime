from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from .settings import Settings

def read_json_file(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None

def load_skills_index(settings: Settings) -> dict[str, Any]:
    return read_json_file(settings.adapter_state_dir / "skills-index.json") or {"skills": []}
