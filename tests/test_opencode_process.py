import asyncio
import json

import pytest

from efp_opencode_adapter.event_bus import EventBus
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

    async def health(self):
        self.calls.append("health_probe")
        return {"healthy": not self.fail_health}


class _WatchdogClient:
    def __init__(self, health_values):
        self.health_values = list(health_values)

    async def health(self):
        if self.health_values:
            return {"healthy": self.health_values.pop(0)}
        return {"healthy": True}


class _WatchdogManager(OpenCodeProcessManager):
    def __init__(self, settings, client, event_bus):
        super().__init__(settings, client, event_bus=event_bus)
        self.restart_reasons = []

    async def restart(self, *, reason="watchdog"):
        self.restart_reasons.append(reason)
        self.process = _FakeProcess()
        self.last_restart_reason = reason
        self.health_ok = True
        return self.status_snapshot()


async def _wait_until(predicate, timeout=0.2):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    assert predicate()


def _capture_managed_spawns(monkeypatch):
    captured_envs: list[dict[str, str]] = []

    async def fake_spawn(*_args, **kwargs):
        captured_envs.append(dict(kwargs.get("env") or {}))
        return _FakeProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_spawn)
    monkeypatch.setattr("efp_opencode_adapter.opencode_process.sync_runtime_skills", lambda _settings: None)
    return captured_envs


@pytest.mark.asyncio
async def test_managed_startup_runs_spawn_then_health_then_registry(monkeypatch):
    calls: list[str] = []

    def fake_sync(_settings):
        calls.append("sync")

    async def fake_spawn(*_args, **_kwargs):
        calls.append("spawn")
        return _FakeProcess()

    async def fake_registry(_settings, _client):
        calls.append("registry")
        return {"status": "ok"}

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_spawn)
    monkeypatch.setattr("efp_opencode_adapter.opencode_process.sync_runtime_skills", fake_sync)
    settings = Settings.from_env()
    manager = OpenCodeProcessManager(settings, _FakeClient(calls), registry_check=fake_registry)
    status = await manager.start({}, reason="startup")
    assert calls == ["sync", "spawn", "health", "registry"]
    assert status["health_ok"] is True
    assert status["registry_ok"] is True
    assert status["last_startup_error"] is None


@pytest.mark.asyncio
async def test_managed_startup_health_failure_skips_registry(monkeypatch):
    calls: list[str] = []

    def fake_sync(_settings):
        calls.append("sync")

    async def fake_spawn(*_args, **_kwargs):
        calls.append("spawn")
        return _FakeProcess()

    async def fake_registry(_settings, _client):
        calls.append("registry")
        return {"status": "ok"}

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_spawn)
    monkeypatch.setattr("efp_opencode_adapter.opencode_process.sync_runtime_skills", fake_sync)
    settings = Settings.from_env()
    manager = OpenCodeProcessManager(settings, _FakeClient(calls, fail_health=True), registry_check=fake_registry)
    with pytest.raises(RuntimeError, match="health failed"):
        await manager.start({}, reason="startup")
    assert calls == ["sync", "spawn", "health"]
    assert manager.health_ok is False
    assert manager.registry_ok is False
    assert "health failed" in (manager.last_startup_error or "")


@pytest.mark.asyncio
async def test_managed_startup_registry_failure_sets_diagnostics(monkeypatch):
    calls: list[str] = []

    def fake_sync(_settings):
        calls.append("sync")

    async def fake_spawn(*_args, **_kwargs):
        calls.append("spawn")
        return _FakeProcess()

    async def fake_registry(_settings, _client):
        calls.append("registry")
        return {"status": "error", "error": "registry failed token=ghu_SECRET"}

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_spawn)
    monkeypatch.setattr("efp_opencode_adapter.opencode_process.sync_runtime_skills", fake_sync)
    settings = Settings.from_env()
    manager = OpenCodeProcessManager(settings, _FakeClient(calls), registry_check=fake_registry)
    with pytest.raises(RuntimeError, match="registry failed"):
        await manager.start({}, reason="startup")
    assert calls == ["sync", "spawn", "health", "registry"]
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
    monkeypatch.setattr("efp_opencode_adapter.opencode_process.sync_runtime_skills", lambda _settings: None)
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    settings = Settings.from_env()
    async def fake_registry(_settings, _client):
        return {"status": "ok"}
    manager = OpenCodeProcessManager(settings, _FakeClient([]), registry_check=fake_registry)
    await manager.start({}, reason="startup")
    assert "--hostname" in captured['args'] and "127.0.0.1" in captured['args']
    assert "--port" in captured['args'] and "4096" in captured['args']
    assert captured['kwargs']["cwd"] == str(settings.workspace_dir)


@pytest.mark.asyncio
async def test_managed_startup_skill_sync_failure_is_recorded(monkeypatch):
    calls: list[str] = []

    def fake_sync(_settings):
        raise ValueError("target skill directory already exists and is not managed by EFP: /workspace/.opencode/skills/demo")

    async def fake_spawn(*_args, **_kwargs):
        calls.append("spawn")
        return _FakeProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_spawn)
    monkeypatch.setattr("efp_opencode_adapter.opencode_process.sync_runtime_skills", fake_sync)
    manager = OpenCodeProcessManager(Settings.from_env(), _FakeClient(calls))
    with pytest.raises(ValueError, match="target skill directory"):
        await manager.start({}, reason="startup")
    assert calls == []
    assert "target skill directory" in (manager.last_startup_error or "")


@pytest.mark.asyncio
async def test_restart_without_env_reuses_last_start_env(monkeypatch):
    captured_envs = _capture_managed_spawns(monkeypatch)
    manager = OpenCodeProcessManager(Settings.from_env(), _FakeClient([]))
    startup_env = {
        "OPENCODE_PROVIDER": "anthropic",
        "ATLASSIAN_CONFIG": "/root/.config/atlassian/config.json",
        "GIT_ASKPASS": "/tmp/askpass",
        "GH_TOKEN": "ghp_SECRET_VALUE",
    }

    await manager.start(startup_env, reason="startup")
    status = await manager.restart(reason="watchdog_process_exited")

    assert captured_envs[1]["OPENCODE_PROVIDER"] == "anthropic"
    assert captured_envs[1]["ATLASSIAN_CONFIG"] == "/root/.config/atlassian/config.json"
    assert captured_envs[1]["GIT_ASKPASS"] == "/tmp/askpass"
    assert status["managed_env_cached"] is True
    assert {"OPENCODE_PROVIDER", "ATLASSIAN_CONFIG", "GIT_ASKPASS", "GH_TOKEN"}.issubset(set(status["managed_env_keys"]))
    encoded = json.dumps(status)
    assert "anthropic" not in encoded
    assert "/tmp/askpass" not in encoded
    assert "ghp_SECRET_VALUE" not in encoded


@pytest.mark.asyncio
async def test_restart_is_watchdog_only_and_always_reuses_boot_env(monkeypatch):
    # No new-env restarts exist anymore: config activation is pod-restart-only,
    # so every managed restart replays the env from the boot-time start.
    import inspect

    captured_envs = _capture_managed_spawns(monkeypatch)
    manager = OpenCodeProcessManager(Settings.from_env(), _FakeClient([]))
    parameters = inspect.signature(manager.restart).parameters
    assert "env" not in parameters

    await manager.start({"OPENCODE_PROVIDER": "boot", "ATLASSIAN_CONFIG": "/boot/config"}, reason="startup")
    await manager.restart(reason="watchdog_health_failed")

    assert captured_envs[0]["OPENCODE_PROVIDER"] == "boot"
    assert captured_envs[1]["OPENCODE_PROVIDER"] == "boot"
    assert captured_envs[1]["ATLASSIAN_CONFIG"] == "/boot/config"


@pytest.mark.asyncio
async def test_watchdog_restart_uses_cached_env(monkeypatch):
    captured_envs = _capture_managed_spawns(monkeypatch)
    manager = OpenCodeProcessManager(Settings.from_env(), _FakeClient([]), event_bus=EventBus())

    await manager.start({"OPENCODE_PROVIDER": "anthropic", "GIT_ASKPASS": "/tmp/askpass"}, reason="startup")
    manager.process.returncode = 1
    task = asyncio.create_task(manager.run_watchdog(interval_seconds=0.001, health_failures_before_restart=2))
    try:
        await _wait_until(lambda: len(captured_envs) >= 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert captured_envs[-1]["OPENCODE_PROVIDER"] == "anthropic"
    assert captured_envs[-1]["GIT_ASKPASS"] == "/tmp/askpass"


@pytest.mark.asyncio
async def test_watchdog_restarts_when_process_exited():
    bus = EventBus()
    sub = bus.subscribe({})
    manager = _WatchdogManager(Settings.from_env(), _WatchdogClient([True]), bus)
    manager.process = _FakeProcess()
    manager.process.returncode = 1
    task = asyncio.create_task(manager.run_watchdog(interval_seconds=0.001, health_failures_before_restart=2))
    try:
        await _wait_until(lambda: manager.restart_reasons)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert manager.restart_reasons == ["watchdog_process_exited"]
    events = []
    while not sub.queue.empty():
        events.append(sub.queue.get_nowait())
    bus.unsubscribe(sub)
    types = [event["type"] for event in events]
    assert "opencode.process.exited" in types
    assert "opencode.process.restarted" in types


@pytest.mark.asyncio
async def test_watchdog_restarts_after_consecutive_health_failures():
    bus = EventBus()
    sub = bus.subscribe({})
    manager = _WatchdogManager(Settings.from_env(), _WatchdogClient([False, False, True]), bus)
    manager.process = _FakeProcess()
    task = asyncio.create_task(manager.run_watchdog(interval_seconds=0.001, health_failures_before_restart=2))
    try:
        await _wait_until(lambda: manager.restart_reasons)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert manager.restart_reasons == ["watchdog_health_failed"]
    events = []
    while not sub.queue.empty():
        events.append(sub.queue.get_nowait())
    bus.unsubscribe(sub)
    types = [event["type"] for event in events]
    assert "opencode.health.failed" in types
    assert "opencode.process.restarted" in types
