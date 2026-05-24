from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit

from .runtime_env import read_runtime_env_file
from .settings import Settings


@dataclass(frozen=True)
class OutboundProxyConfig:
    proxy_url: str | None
    trust_env: bool = True


def _runtime_env(settings: Settings) -> dict[str, str]:
    return read_runtime_env_file(settings.adapter_state_dir / "opencode.env")


def _first_proxy_value(runtime_env: dict[str, str], process_env: Mapping[str, str], keys: tuple[str, ...]) -> str | None:
    for source in (runtime_env, process_env):
        for key in keys:
            value = str(source.get(key) or "").strip()
            if value:
                return value
    return None


def _split_no_proxy(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[,\s]+", value or "") if item.strip()]


def _default_port(scheme: str) -> int | None:
    if scheme == "http":
        return 80
    if scheme == "https":
        return 443
    return None


def _url_port(parts) -> int | None:
    try:
        return parts.port
    except ValueError:
        return None


def _no_proxy_entry_host_port(entry: str) -> tuple[str, int | None]:
    parsed = urlsplit(entry if "://" in entry else f"//{entry}")
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    try:
        port = parsed.port
    except ValueError:
        port = None
    return host, port


def _host_matches_no_proxy_entry(host: str, entry_host: str) -> bool:
    if not entry_host:
        return False
    if entry_host.startswith("*."):
        entry_host = entry_host[1:]
    if entry_host.startswith("."):
        suffix = entry_host[1:]
        return host == suffix or host.endswith(f".{suffix}")
    return host == entry_host or host.endswith(f".{entry_host}")


def _no_proxy_matches(no_proxy: str | None, target_url: str) -> bool:
    if not no_proxy:
        return False
    parts = urlsplit(target_url)
    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host:
        return False
    target_port = _url_port(parts) or _default_port(parts.scheme.lower())
    for entry in _split_no_proxy(no_proxy):
        if entry == "*":
            return True
        entry_host, entry_port = _no_proxy_entry_host_port(entry)
        if entry_port is not None and target_port != entry_port:
            continue
        if _host_matches_no_proxy_entry(host, entry_host):
            return True
    return False


def outbound_proxy_config_for_url(settings: Settings, target_url: str) -> OutboundProxyConfig:
    runtime_env = _runtime_env(settings)
    no_proxy = _first_proxy_value(runtime_env, os.environ, ("NO_PROXY", "no_proxy"))
    if _no_proxy_matches(no_proxy, target_url):
        return OutboundProxyConfig(proxy_url=None, trust_env=False)

    scheme = urlsplit(target_url).scheme.lower()
    if scheme == "https":
        keys = ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy")
    elif scheme == "http":
        keys = ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")
    else:
        return OutboundProxyConfig(proxy_url=None)

    return OutboundProxyConfig(proxy_url=_first_proxy_value(runtime_env, os.environ, keys))


def outbound_proxy_for_url(settings: Settings, target_url: str) -> str | None:
    return outbound_proxy_config_for_url(settings, target_url).proxy_url
