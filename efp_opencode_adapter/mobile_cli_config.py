from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .path_utils import path_exists
from .settings import Settings

_REDACTED_VALUES = {"***redacted***", "[redacted]", "redacted"}


@dataclass(frozen=True)
class MobileCLIConfigResult:
    configured: bool
    path: str
    env: dict[str, str]
    updated_sections: list[str]
    warnings: list[str]
    redacted_status: dict[str, Any]


def _section_enabled(section: Any) -> bool:
    return isinstance(section, dict) and section.get("enabled") is not False


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _clean_secret(value: Any) -> str:
    text = _clean_text(value)
    return "" if not text or text.lower() in _REDACTED_VALUES else text


def _copy_jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value))


def _path_text(path: Path) -> str:
    return path.as_posix()


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path_exists(path):
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _chmod_best_effort(path: Path, mode: int, warnings: list[str], warning: str) -> None:
    try:
        path.chmod(mode)
    except OSError:
        warnings.append(warning)


def _build_mobile_config(settings: Settings, runtime_config: dict, warnings: list[str]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    source = runtime_config.get("mobile") if isinstance(runtime_config.get("mobile"), dict) else {}
    status: dict[str, Any] = {"configured": False}
    if not _section_enabled(source):
        return None, status

    mobile = _copy_jsonable(source)
    mobile.pop("enabled", None)
    mobile.setdefault("state_dir", str(settings.mobile_state_dir))
    mobile.setdefault("artifacts_dir", str(settings.mobile_artifacts_dir))

    browserstack = mobile.get("browserstack") if isinstance(mobile.get("browserstack"), dict) else {}
    if browserstack:
        username = _clean_text(browserstack.get("username"))
        access_key = _clean_secret(browserstack.get("access_key"))
        if username:
            browserstack["username"] = username
        else:
            browserstack.pop("username", None)
        if access_key:
            browserstack["access_key"] = access_key
        else:
            browserstack.pop("access_key", None)

        local = browserstack.get("local") if isinstance(browserstack.get("local"), dict) else {}
        if local is not None:
            local.setdefault("binary", _path_text(settings.browserstack_local_binary_path))
            browserstack["local"] = local
        mobile["browserstack"] = browserstack

    status = {
        "configured": True,
        "state_dir": mobile.get("state_dir"),
        "artifacts_dir": mobile.get("artifacts_dir"),
        "browserstack": {
            "username_present": bool(browserstack.get("username") or browserstack.get("username_env")),
            "access_key_present": bool(browserstack.get("access_key") or browserstack.get("access_key_env")),
            "api_base_url": browserstack.get("api_base_url"),
            "appium_base_url": browserstack.get("appium_base_url"),
        },
        "local": {
            "mode": (browserstack.get("local") or {}).get("mode") if isinstance(browserstack.get("local"), dict) else None,
            "binary": str((browserstack.get("local") or {}).get("binary") or _path_text(settings.browserstack_local_binary_path))
            if isinstance(browserstack.get("local"), dict)
            else _path_text(settings.browserstack_local_binary_path),
            "binary_present": path_exists(settings.browserstack_local_binary_path),
        },
    }
    if not browserstack:
        warnings.append("mobile enabled but browserstack config is missing")
    return mobile, status


def write_mobile_cli_config(settings: Settings, runtime_config: dict) -> MobileCLIConfigResult:
    warnings: list[str] = []
    path = settings.efp_config_path
    env = {
        "EFP_CONFIG": str(path),
        "MOBILE_STATE_DIR": str(settings.mobile_state_dir),
        "MOBILE_ARTIFACTS_DIR": str(settings.mobile_artifacts_dir),
        "BROWSERSTACK_LOCAL_BINARY": _path_text(settings.browserstack_local_binary_path),
    }
    mobile_config, status = _build_mobile_config(settings, runtime_config if isinstance(runtime_config, dict) else {}, warnings)
    existing = _read_yaml_mapping(path)
    existing.pop("mobile", None)
    configured = mobile_config is not None
    if configured:
        existing["mobile"] = mobile_config

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _chmod_best_effort(path.parent, 0o700, warnings, "unable to set EFP config directory permissions")
    except OSError as exc:
        raise OSError("unable to create EFP config directory") from exc

    if existing:
        tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp_path.write_text(yaml.safe_dump(existing, sort_keys=False, allow_unicode=True), encoding="utf-8")
        _chmod_best_effort(tmp_path, 0o600, warnings, "unable to set EFP config file permissions")
        tmp_path.replace(path)
        _chmod_best_effort(path, 0o600, warnings, "unable to set EFP config file permissions")
    elif path.exists():
        try:
            path.unlink()
        except OSError:
            warnings.append("unable to remove stale EFP config file")

    return MobileCLIConfigResult(
        configured=configured,
        path=str(path),
        env=env,
        updated_sections=["mobile"] if isinstance((runtime_config or {}).get("mobile"), dict) else [],
        warnings=warnings,
        redacted_status=status,
    )
