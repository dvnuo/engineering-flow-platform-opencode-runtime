import json
from pathlib import Path


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_locked_version(lock_payload: dict, package_name: str) -> str | None:
    package_key = f"node_modules/{package_name}"
    version = ((lock_payload.get("packages") or {}).get(package_key) or {}).get("version")
    if version is not None:
        return version
    return ((lock_payload.get("dependencies") or {}).get(package_name) or {}).get("version")


def test_package_lock_tracks_declared_opencode_package_versions():
    root = Path(__file__).resolve().parents[1]
    lock_path = root / "package-lock.json"
    package_json_path = root / "package.json"

    assert lock_path.exists(), "package-lock.json is required by T05"

    lock_payload = _read_json(lock_path)
    package_payload = _read_json(package_json_path)
    dependencies = package_payload.get("dependencies") or {}

    for package_name in ("opencode-ai",):
        declared_version = dependencies.get(package_name)
        assert declared_version is not None
        locked_version = _resolve_locked_version(lock_payload, package_name)
        assert locked_version == declared_version
