from __future__ import annotations

from pathlib import Path


def path_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False
