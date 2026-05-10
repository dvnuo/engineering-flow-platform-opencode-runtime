import pytest

from efp_opencode_adapter.opencode_process import OpenCodeProcessManager
from efp_opencode_adapter.settings import Settings


class _FakeProcess:
    def __init__(self) -> None:
        self.pid = 123
        self.returncode = None

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        self.returncode = 0
        return 0


class _FakeClient:
    def __init__(self, calls: list[str], fail_health: bool = False) -> None:
        self.calls = calls
        self.fail_health = fail_health

    async def wait_until_ready(self, _timeout_seconds: int) -> None:
        self.calls.append("health")
        if self.fail_health:
            raise RuntimeError("health failed")


@pytest.mark.asyncio
async def test_managed_startup_runs_spawn_then_health_then_registry(monkeypatch):
    calls: list[str] = []

    async def fake_spawn(*_args, **_kwargs):
        calls.append("spawn")
        return _FakeProcess()

    async def fake_registry(_settings, _client):
        calls.append("registry")
        return {"status": "ok"}

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_spawn)
    settings = Settings.from_env()
    manager = OpenCodeProcessManager(settings, _FakeClient(calls), registry_check=fake_registry)
    status = await manager.start({}, reason="startup")
    assert calls == ["spawn", "health", "registry"]
    assert status["health_ok"] is True
    assert status["registry_ok"] is True
    assert status["last_startup_error"] is None


@pytest.mark.asyncio
async def test_managed_startup_health_failure_skips_registry(monkeypatch):
    calls: list[str] = []

    async def fake_spawn(*_args, **_kwargs):
        calls.append("spawn")
        return _FakeProcess()

    async def fake_registry(_settings, _client):
        calls.append("registry")
        return {"status": "ok"}

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_spawn)
    settings = Settings.from_env()
    manager = OpenCodeProcessManager(settings, _FakeClient(calls, fail_health=True), registry_check=fake_registry)
    with pytest.raises(RuntimeError, match="health failed"):
        await manager.start({}, reason="startup")
    assert calls == ["spawn", "health"]
    assert manager.health_ok is False
    assert manager.registry_ok is False
    assert "health failed" in (manager.last_startup_error or "")


@pytest.mark.asyncio
async def test_managed_startup_registry_failure_sets_diagnostics(monkeypatch):
    calls: list[str] = []

    async def fake_spawn(*_args, **_kwargs):
        calls.append("spawn")
        return _FakeProcess()

    async def fake_registry(_settings, _client):
        calls.append("registry")
        return {"status": "error", "error": "registry failed token=ghu_SECRET"}

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_spawn)
    settings = Settings.from_env()
    manager = OpenCodeProcessManager(settings, _FakeClient(calls), registry_check=fake_registry)
    with pytest.raises(RuntimeError, match="registry failed"):
        await manager.start({}, reason="startup")
    assert calls == ["spawn", "health", "registry"]
    assert manager.health_ok is False
    assert manager.registry_ok is False
    assert "registry failed" in (manager.last_startup_error or "")
    assert "ghu_SECRET" not in (manager.last_startup_error or "")

@pytest.mark.asyncio
async def test_spawn_uses_workspace_cwd_and_localhost_port(monkeypatch, tmp_path):
    captured = {}

    async def fake_spawn(*args, **kwargs):
        captured['args'] = args
        captured['kwargs'] = kwargs
        return _FakeProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_spawn)
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    settings = Settings.from_env()
    async def fake_registry(_settings, _client):
        return {"status": "ok"}
    manager = OpenCodeProcessManager(settings, _FakeClient([]), registry_check=fake_registry)
    await manager.start({}, reason="startup")
    assert "--hostname" in captured['args'] and "127.0.0.1" in captured['args']
    assert "--port" in captured['args'] and "4096" in captured['args']
    assert captured['kwargs']["cwd"] == str(settings.workspace_dir)
