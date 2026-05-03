import json
from pathlib import Path


def test_package_lock_pins_opencode_ai_1_14_29():
    root = Path(__file__).resolve().parents[1]
    lock_path = root / "package-lock.json"

    assert lock_path.exists(), "package-lock.json is required by T05"

    payload = json.loads(lock_path.read_text(encoding="utf-8"))
    version = (
        (payload.get("packages") or {})
        .get("node_modules/opencode-ai", {})
        .get("version")
    )

    if version is None:
        version = (
            (payload.get("dependencies") or {})
            .get("opencode-ai", {})
            .get("version")
        )

    assert version == "1.14.29"


def test_package_lock_pins_plugin_1_14_29():
    root = Path(__file__).resolve().parents[1]
    lock_path = root / "package-lock.json"

    payload = json.loads(lock_path.read_text(encoding="utf-8"))
    version = ((payload.get("packages") or {}).get("node_modules/@opencode-ai/plugin", {}).get("version"))
    if version is None:
        version = ((payload.get("dependencies") or {}).get("@opencode-ai/plugin", {}).get("version"))

    assert version == "1.14.29"
