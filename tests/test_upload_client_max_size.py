"""Upload size: opencode adapter aiohttp client_max_size is env-configurable.

Parity with the native runtime: the adapter app previously used aiohttp's 1MB
default, capping uploads regardless of the Portal limit. These lock the
EFP_MAX_UPLOAD_MB wiring and its transport headroom.
"""

from pathlib import Path

from efp_opencode_adapter import server
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings

_HEADROOM = server.UPLOAD_TRANSPORT_HEADROOM_MB
_DEFAULT = server.DEFAULT_MAX_UPLOAD_MB


def test_default_client_max_size(monkeypatch):
    monkeypatch.delenv("EFP_MAX_UPLOAD_MB", raising=False)
    assert server.resolve_upload_client_max_size() == (_DEFAULT + _HEADROOM) * 1024 * 1024


def test_env_override(monkeypatch):
    monkeypatch.setenv("EFP_MAX_UPLOAD_MB", "50")
    assert server.resolve_upload_client_max_size() == (50 + _HEADROOM) * 1024 * 1024


def test_invalid_value_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("EFP_MAX_UPLOAD_MB", "nope")
    assert server.resolve_upload_client_max_size() == (_DEFAULT + _HEADROOM) * 1024 * 1024


def test_non_positive_value_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("EFP_MAX_UPLOAD_MB", "-3")
    assert server.resolve_upload_client_max_size() == (_DEFAULT + _HEADROOM) * 1024 * 1024


def test_default_matches_native_runtime():
    # Both runtimes must default identically so agent behaviour is consistent.
    assert _DEFAULT == 25 and _HEADROOM == 5


def test_create_app_is_wired_with_client_max_size():
    # Asserted on the built app (not the source text) so the wiring stays locked
    # no matter which other web.Application(...) arguments are added.
    source = Path("efp_opencode_adapter/server.py").read_text(encoding="utf-8")
    assert "client_max_size=resolve_upload_client_max_size()" in source
    app = create_app(Settings.from_env())
    assert app._client_max_size == server.resolve_upload_client_max_size()
