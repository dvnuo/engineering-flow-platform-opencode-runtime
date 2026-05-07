from __future__ import annotations

from pathlib import Path


SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    "dist",
    "build",
}


def _is_binary_file(path: Path) -> bool:
    try:
        chunk = path.read_bytes()[:4096]
    except OSError:
        return True
    if not chunk:
        return False
    return b"\x00" in chunk


def _forbidden_names() -> list[str]:
    prefix = "OPENCODE_" + "SERVER_"
    return [prefix + "USERNAME", prefix + "PASSWORD"]


def _iter_repo_files(root: Path):
    for path in root.rglob("*"):
        if path.is_dir():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix == ".pyc":
            continue
        if _is_binary_file(path):
            continue
        yield path


def test_repo_does_not_contain_removed_internal_server_credential_names():
    root = Path(__file__).resolve().parents[1]
    forbidden = _forbidden_names()
    hits: list[tuple[Path, str]] = []

    for path in _iter_repo_files(root):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for token in forbidden:
            if token in text:
                hits.append((path.relative_to(root), token))

    assert not hits, "Found disallowed names: " + ", ".join(f"{file_path} -> {token}" for file_path, token in hits)
