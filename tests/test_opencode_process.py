import asyncio
import json
import logging
import sys
import time

import pytest

from efp_opencode_adapter import opencode_process
from efp_opencode_adapter.event_bus import EventBus
from efp_opencode_adapter.opencode_process import (
    CHILD_LOG_PUMP_MAX_DRAIN_SECONDS,
    LOG_TAIL_PREVIEW_CHARS,
    OpenCodeProcessManager,
)
from efp_opencode_adapter.settings import Settings


class _FakeStream:
    """Minimal StreamReader stand-in: yields queued lines (or raises a queued
    exception) then EOF."""

    def __init__(self, items) -> None:
        self.items = list(items)

    async def readline(self) -> bytes:
        if not self.items:
            return b""
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


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


def _spawn_with_output(monkeypatch, items):
    """Spawn a fake child whose merged stdout/stderr emits ``items``."""
    process = _FakeProcess()
    process.stdout = _FakeStream(items)

    async def fake_spawn(*_args, **kwargs):
        assert kwargs["stdout"] == asyncio.subprocess.PIPE
        assert kwargs["stderr"] == asyncio.subprocess.STDOUT
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_spawn)
    monkeypatch.setattr("efp_opencode_adapter.opencode_process.sync_runtime_skills", lambda _settings: None)
    return process


@pytest.mark.asyncio
async def test_child_output_is_reemitted_on_adapter_stdout_and_log_file(monkeypatch, tmp_path, caplog):
    log_file = tmp_path / "opencode-serve.log"
    monkeypatch.setenv("OPENCODE_LOG_FILE", str(log_file))
    _spawn_with_output(monkeypatch, [b"serving on 4096\n", b"snapshot done\n"])
    manager = OpenCodeProcessManager(Settings.from_env(), _FakeClient([]))

    with caplog.at_level(logging.INFO, logger="efp_opencode_adapter.opencode_process"):
        await manager.start({}, reason="startup")
        await manager.stop()

    messages = [record.getMessage() for record in caplog.records]
    assert "opencode: serving on 4096" in messages
    assert "opencode: snapshot done" in messages
    # The log file on the PVC keeps its previous content contract.
    assert log_file.read_text(encoding="utf-8").splitlines() == ["serving on 4096", "snapshot done"]
    # log_tail() is served from the in-memory ring, so it is fresh and bounded.
    assert manager.log_tail(10).splitlines() == ["serving on 4096", "snapshot done"]


@pytest.mark.asyncio
async def test_child_output_secrets_are_redacted_before_stdout(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("OPENCODE_LOG_FILE", str(tmp_path / "opencode-serve.log"))
    _spawn_with_output(monkeypatch, [b"auth token=ghu_SUPERSECRET ok\n"])
    manager = OpenCodeProcessManager(Settings.from_env(), _FakeClient([]))

    with caplog.at_level(logging.INFO, logger="efp_opencode_adapter.opencode_process"):
        await manager.start({}, reason="startup")
        await manager.stop()

    forwarded = [m for m in (r.getMessage() for r in caplog.records) if m.startswith("opencode: ")]
    assert forwarded == ["opencode: auth token=ghu_***REDACTED*** ok"]


# A child that dumps a burst and dies: the burst must not push the fatal last
# line out of the log the feature exists to make readable.
_BURST_LINES = 20000
_BURST_MARKER = "FINAL-CRASH-MARKER"
_BURST_SCRIPT = (
    "import sys\n"
    "out = sys.stdout\n"
    "body = 'x' * 190\n"
    f"for i in range({_BURST_LINES}):\n"
    "    out.write('%s%06d\\n' % (body, i))\n"
    f"out.write('{_BURST_MARKER}\\n')\n"
    "out.flush()\n"
)


def _spawn_real_child(monkeypatch, script: str):
    """Spawn a real python child in place of `opencode serve`, keeping the pipe
    wiring the manager asked for."""
    real_spawn = asyncio.create_subprocess_exec

    async def fake_spawn(*_args, **kwargs):
        return await real_spawn(
            sys.executable,
            "-c",
            script,
            stdout=kwargs["stdout"],
            stderr=kwargs["stderr"],
            limit=kwargs["limit"],
        )

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_spawn)
    monkeypatch.setattr("efp_opencode_adapter.opencode_process.sync_runtime_skills", lambda _settings: None)


@pytest.mark.asyncio
async def test_stop_keeps_the_last_line_of_a_child_burst(monkeypatch, tmp_path):
    log_file = tmp_path / "opencode-serve.log"
    monkeypatch.setenv("OPENCODE_LOG_FILE", str(log_file))
    _spawn_real_child(monkeypatch, _BURST_SCRIPT)
    manager = OpenCodeProcessManager(Settings.from_env(), _FakeClient([]))

    await manager.start({}, reason="startup")
    await asyncio.wait_for(manager.process.wait(), timeout=120)
    await manager.stop()

    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert lines[-1] == _BURST_MARKER
    assert len(lines) == _BURST_LINES + 1
    assert _BURST_MARKER in manager.log_tail(5)


def _attach_pump(manager, log_file, *, returncode=1):
    """Wire a live pump onto a StreamReader the test drives by hand."""
    stream = asyncio.StreamReader()
    process = _FakeProcess()
    process.stdout = stream
    process.returncode = returncode
    manager.process = process
    manager.log_path = log_file
    manager._start_output_pump(process, log_file.open("ab", buffering=64 * 1024))
    return stream


@pytest.mark.asyncio
async def test_drain_keeps_the_whole_burst_a_dead_child_left_in_the_pipe(monkeypatch, tmp_path):
    """A child that dumps a stack trace and dies out-runs the pump: the drain
    must keep going until the buffered burst is fully written, not stop at some
    fixed budget that discards exactly the fatal tail."""
    log_file = tmp_path / "opencode-serve.log"
    monkeypatch.setenv("OPENCODE_LOG_FILE", str(log_file))
    manager = OpenCodeProcessManager(Settings.from_env(), _FakeClient([]))
    stream = _attach_pump(manager, log_file)

    buffered = 5000
    body = "x" * 190
    for index in range(buffered):
        stream.feed_data(f"{body}{index:06d}\n".encode("utf-8"))
    stream.feed_data(f"{_BURST_MARKER}\n".encode("utf-8"))
    stream.feed_eof()

    await manager.stop()

    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert lines[-1] == _BURST_MARKER
    assert len(lines) == buffered + 1


@pytest.mark.asyncio
async def test_an_orphan_holding_the_pipe_cannot_hold_the_drain_or_stop_open(monkeypatch, tmp_path):
    """`opencode serve` hands the merged pipe to every tool subprocess it
    spawns. When opencode dies mid-tool the orphan keeps the pipe open and keeps
    writing, so progress never stalls and EOF never arrives. That must not hold
    stop()/restart() (and therefore the watchdog's revive) open."""
    log_file = tmp_path / "opencode-serve.log"
    monkeypatch.setenv("OPENCODE_LOG_FILE", str(log_file))
    manager = OpenCodeProcessManager(Settings.from_env(), _FakeClient([]))

    async def keep_writing(stream):
        index = 0
        while True:
            await asyncio.sleep(0.1)
            stream.feed_data(f"orphan-tool-line-{index}\n".encode("utf-8"))
            index += 1

    stream = _attach_pump(manager, log_file)
    orphan = asyncio.create_task(keep_writing(stream))
    started = time.perf_counter()
    try:
        await manager._drain_output_pump(max_seconds=12.0)
    finally:
        orphan.cancel()
        await asyncio.gather(orphan, return_exceptions=True)
    drain_seconds = time.perf_counter() - started
    assert drain_seconds < 6.0, f"orphan held the drain for {drain_seconds:.1f}s"

    # ... and the same bound applies through the stop() the watchdog restart
    # runs, whose default ceiling is two minutes.
    stream = _attach_pump(manager, log_file)
    orphan = asyncio.create_task(keep_writing(stream))
    started = time.perf_counter()
    try:
        await manager.stop()
    finally:
        orphan.cancel()
        await asyncio.gather(orphan, return_exceptions=True)
    stop_seconds = time.perf_counter() - started
    assert stop_seconds < 6.0, f"orphan held stop() for {stop_seconds:.1f}s"
    assert CHILD_LOG_PUMP_MAX_DRAIN_SECONDS >= 60.0

    # Whatever the orphan wrote before the drain gave up still reached the log.
    assert "orphan-tool-line-0" in log_file.read_text(encoding="utf-8")


def _opencode_debug_line(index: int) -> bytes:
    """A line shaped like the JSON debug output opencode relays from a build."""
    payload = json.dumps(
        {
            "level": "DEBUG",
            "service": "opencode",
            "session": f"ses_7f3a{index:05d}",
            "msg": "tool call finished",
            "tool": "bash",
            "duration_ms": 1234,
            "usage": {"tokens": {"input": 10, "output": 20}},
            "output": "y" * 380,
        }
    )
    return (payload + "\n").encode("utf-8")


class _BufferedBurstStream:
    """A stream whose data is already in the pipe buffer: readline() returns
    without ever suspending, exactly like a StreamReader with a full buffer."""

    def __init__(self, count: int) -> None:
        self.remaining = count
        self.index = 0

    async def readline(self) -> bytes:
        if self.remaining <= 0:
            return b""
        self.remaining -= 1
        self.index += 1
        return _opencode_debug_line(self.index)


@pytest.mark.asyncio
async def test_pump_yields_the_event_loop_while_draining_a_burst(tmp_path):
    """readline() does not suspend while the pipe buffer is non-empty, so
    without an explicit yield the pump owns the loop for the whole burst and
    /health, /ready, SSE delivery and the watchdog probe stall behind it."""
    log_file = tmp_path / "opencode-serve.log"
    manager = OpenCodeProcessManager(Settings.from_env(), _FakeClient([]))
    manager.log_path = log_file
    process = _FakeProcess()
    stream = _BufferedBurstStream(8000)
    handle = log_file.open("ab", buffering=64 * 1024)

    gaps: list[float] = []

    async def loop_lag_probe():
        while True:
            mark = time.perf_counter()
            await asyncio.sleep(0)
            gaps.append(time.perf_counter() - mark)

    probe = asyncio.create_task(loop_lag_probe())
    await asyncio.sleep(0)
    started = time.perf_counter()
    try:
        await manager._pump_child_output(process, stream, handle)
    finally:
        probe.cancel()
        await asyncio.gather(probe, return_exceptions=True)
    pump_seconds = time.perf_counter() - started

    assert log_file.read_text(encoding="utf-8").count("\n") == 8000
    # The burst is real work, so a pump that never yields is plainly visible.
    assert pump_seconds > 0.1, f"burst too cheap to measure ({pump_seconds:.3f}s)"
    assert len(gaps) >= 100, f"only {len(gaps)} loop ticks during a 8000-line burst"
    assert max(gaps) < 0.25, f"longest event-loop stall was {max(gaps) * 1000:.0f}ms"


@pytest.mark.asyncio
async def test_child_log_writes_are_not_handed_to_the_executor_per_line(monkeypatch, tmp_path):
    """Only the flush (the PVC-touching part) may leave the loop; a hand-off per
    line turns the adapter into a throughput valve on the child."""
    log_file = tmp_path / "opencode-serve.log"
    monkeypatch.setenv("OPENCODE_LOG_FILE", str(log_file))
    line_count = 500
    _spawn_with_output(monkeypatch, [b"line-%d\n" % index for index in range(line_count)])

    handoffs: list[str] = []
    real_to_thread = asyncio.to_thread

    async def counting_to_thread(func, /, *args, **kwargs):
        handoffs.append(getattr(func, "__name__", str(func)))
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", counting_to_thread)
    manager = OpenCodeProcessManager(Settings.from_env(), _FakeClient([]))

    await manager.start({}, reason="startup")
    await manager.stop()

    assert log_file.read_text(encoding="utf-8").splitlines() == [f"line-{i}" for i in range(line_count)]
    assert len(handoffs) <= 2, f"{len(handoffs)} executor hand-offs for {line_count} child lines"
    assert all(name == "_flush_child_log" for name in handoffs)


def _clear_adapter_secret_env(monkeypatch) -> None:
    """In the profile-Secret architecture these never sit in the adapter's own
    environment; redaction must not depend on them."""
    for key in ("PORTAL_INTERNAL_TOKEN", "OPENAI_API_KEY", "GITHUB_TOKEN", "GH_TOKEN", "JIRA_API_TOKEN"):
        monkeypatch.delenv(key, raising=False)


async def _forwarded_lines(monkeypatch, tmp_path, caplog, line: str, start_env: dict) -> list[str]:
    monkeypatch.setenv("OPENCODE_LOG_FILE", str(tmp_path / "opencode-serve.log"))
    _spawn_with_output(monkeypatch, [(line + "\n").encode("utf-8")])
    manager = OpenCodeProcessManager(Settings.from_env(), _FakeClient([]))
    with caplog.at_level(logging.INFO, logger="efp_opencode_adapter.opencode_process"):
        await manager.start(start_env, reason="startup")
        await manager.stop()
    forwarded = [m for m in (r.getMessage() for r in caplog.records) if m.startswith("opencode: ")]
    assert forwarded, "child line never reached the logger"
    # log_tail() feeds last_startup_error, so it must be redacted too.
    forwarded.append(manager.log_tail(10))
    return forwarded


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "env_key,secret",
    [
        ("EFP_JENKINS_0_PASSWORD", "Jenk1nsBuildBotValue42"),
        ("EFP_JIRA_0_API_TOKEN", "AtlassianProfileTokenValue99"),
        ("GIT_PASSWORD", "GitProfileCredentialValue77"),
        ("BROWSERSTACK_ACCESS_KEY", "BrowserStackProfileKey55"),
    ],
)
async def test_child_output_redacts_secrets_taken_from_the_spawn_env(
    monkeypatch, tmp_path, caplog, env_key, secret
):
    _clear_adapter_secret_env(monkeypatch)
    forwarded = await _forwarded_lines(
        monkeypatch,
        tmp_path,
        caplog,
        f"ERROR upstream rejected credential {secret} for user bot",
        {env_key: secret},
    )
    assert secret not in "\n".join(forwarded)
    assert "***REDACTED***" in forwarded[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "line,secret",
    [
        ("remote: auth failed for ghp_AbCd1234567890abcdefGHIJKLmnopq", "ghp_AbCd1234567890abcdefGHIJKLmnopq"),
        ("oauth gho_AbCd1234567890abcdefGHIJKLmnopq expired", "gho_AbCd1234567890abcdefGHIJKLmnopq"),
        ("user token ghu_AbCd1234567890abcdefGHIJKLmnopq", "ghu_AbCd1234567890abcdefGHIJKLmnopq"),
        ("server token ghs_AbCd1234567890abcdefGHIJKLmnopq", "ghs_AbCd1234567890abcdefGHIJKLmnopq"),
        (
            "fine grained github_pat_11ABCDE0A0aBcDeFgHiJkL_0123456789abcdefXYZ used",
            "github_pat_11ABCDE0A0aBcDeFgHiJkL_0123456789abcdefXYZ",
        ),
        ("openai key sk-proj-Ab12Cd34Ef56Gh78Ij90 denied", "sk-proj-Ab12Cd34Ef56Gh78Ij90"),
        ("jira 401 ATATT3xFfGF0T4abcdEFGH12345ijkl", "ATATT3xFfGF0T4abcdEFGH12345ijkl"),
        ("aws sts key AKIAIOSFODNN7EXAMPLE rejected", "AKIAIOSFODNN7EXAMPLE"),
        (
            "aws secret wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY rejected",
            "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        ),
        ("jenkins login password=Hunter2Hunter2 failed", "Hunter2Hunter2"),
        ("POST /api authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig", "eyJhbGciOiJIUzI1NiJ9.payload.sig"),
        (
            "fatal: clone https://x-access-token:ghp_AbCd1234567890abcdefGHIJ@github.example.com/o/r failed",
            "ghp_AbCd1234567890abcdefGHIJ",
        ),
    ],
)
async def test_child_output_redacts_credential_shapes_absent_from_every_env(
    monkeypatch, tmp_path, caplog, line, secret
):
    """Fail closed: the profile Secret values the adapter never sees still must
    not reach pod stdout when opencode echoes them."""
    _clear_adapter_secret_env(monkeypatch)
    forwarded = await _forwarded_lines(monkeypatch, tmp_path, caplog, line, {})
    assert secret not in "\n".join(forwarded), forwarded[0]
    assert "***REDACTED***" in forwarded[0]


_TELEMETRY_LINES = [
    "INFO session=ses_1 tokens=1523 input_tokens=900 output_tokens=623",
    "INFO tokenizer=cl100k_base token_count=42",
    "INFO secret=false debug=true",
    'INFO usage {"tokens":{"input":10,"output":20}}',
    "INFO cache_read_tokens=0 reasoning_tokens=3 max_tokens=4096",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("line", _TELEMETRY_LINES)
async def test_ordinary_telemetry_survives_redaction_verbatim(monkeypatch, tmp_path, caplog, line):
    """Token accounting and usage lines are among the most common lines an LLM
    runtime emits; redacting them guts the observability the forwarding exists
    to provide."""
    _clear_adapter_secret_env(monkeypatch)
    forwarded = await _forwarded_lines(monkeypatch, tmp_path, caplog, line, {})
    assert forwarded[0] == f"opencode: {line}"


@pytest.mark.asyncio
async def test_redacted_json_log_lines_stay_parseable(monkeypatch, tmp_path, caplog):
    _clear_adapter_secret_env(monkeypatch)
    line = json.dumps(
        {
            "level": "INFO",
            "usage": {"tokens": {"input": 10, "output": 20}},
            "api_token": "OpaqueCredentialValue",
            "input_tokens": 900,
        }
    )
    forwarded = await _forwarded_lines(monkeypatch, tmp_path, caplog, line, {})
    emitted = forwarded[0][len("opencode: ") :]
    assert "OpaqueCredentialValue" not in emitted
    decoded = json.loads(emitted)
    assert decoded["api_token"] == "***REDACTED***"
    assert decoded["usage"] == {"tokens": {"input": 10, "output": 20}}
    assert decoded["input_tokens"] == 900


@pytest.mark.asyncio
async def test_a_secret_key_holding_an_object_does_not_swallow_the_json(monkeypatch, tmp_path, caplog):
    """A secret-shaped key whose value is a nested object must not consume the
    opening brace: that both breaks the line and leaves the object's own
    contents outside the redacted span."""
    _clear_adapter_secret_env(monkeypatch)
    line = json.dumps(
        {
            "level": "ERROR",
            "credentials": {"user": "bot", "password": "OpaqueCredentialValue"},
            "input_tokens": 900,
        }
    )
    forwarded = await _forwarded_lines(monkeypatch, tmp_path, caplog, line, {})
    emitted = forwarded[0][len("opencode: ") :]
    assert "OpaqueCredentialValue" not in emitted
    decoded = json.loads(emitted)
    assert decoded["credentials"]["user"] == "bot"
    assert decoded["credentials"]["password"] == "***REDACTED***"
    assert decoded["input_tokens"] == 900


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "line",
    [
        "auth token=OpaqueCredentialValue rejected",
        "auth api_token=OpaqueCredentialValue rejected",
        "auth access_token: OpaqueCredentialValue rejected",
        "auth x-api-key=OpaqueCredentialValue rejected",
        "env AWS_SECRET_ACCESS_KEY=OpaqueCredentialValue exported",
        "jenkins password=OpaqueCredentialValue failed",
        'jenkins {"credentials": "OpaqueCredentialValue"} failed',
    ],
)
async def test_child_output_still_redacts_whole_key_credentials(monkeypatch, tmp_path, caplog, line):
    """The shapeless credentials (an opaque profile value with no prefix) are
    only caught by the key/value sweep, so whole-key matching must not narrow
    it away from the keys that really are credentials."""
    _clear_adapter_secret_env(monkeypatch)
    forwarded = await _forwarded_lines(monkeypatch, tmp_path, caplog, line, {})
    assert "OpaqueCredentialValue" not in "\n".join(forwarded)
    assert "***REDACTED***" in forwarded[0]


@pytest.mark.asyncio
async def test_child_output_pump_survives_an_overlong_line(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("OPENCODE_LOG_FILE", str(tmp_path / "opencode-serve.log"))
    _spawn_with_output(monkeypatch, [ValueError("Separator is not found, and chunk exceed the limit"), b"after\n"])
    manager = OpenCodeProcessManager(Settings.from_env(), _FakeClient([]))

    with caplog.at_level(logging.INFO, logger="efp_opencode_adapter.opencode_process"):
        await manager.start({}, reason="startup")
        await manager.stop()

    messages = [record.getMessage() for record in caplog.records]
    assert any(m.startswith("opencode.log.line_dropped") for m in messages)
    assert "opencode: after" in messages


@pytest.mark.asyncio
async def test_startup_error_tail_uses_forwarded_child_output(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCODE_LOG_FILE", str(tmp_path / "opencode-serve.log"))
    _spawn_with_output(monkeypatch, [b"fatal: port busy\n"])
    manager = OpenCodeProcessManager(Settings.from_env(), _FakeClient([], fail_health=True))

    with pytest.raises(RuntimeError, match="health failed"):
        await manager.start({}, reason="startup")
    await manager.stop()

    assert "fatal: port busy" in (manager.last_startup_error or "")


def test_log_tail_reads_only_the_end_of_a_large_log_file(monkeypatch, tmp_path):
    log_file = tmp_path / "opencode-serve.log"
    filler = "\n".join(f"noise-{i}" for i in range(200_000))
    log_file.write_text(filler + "\nlast-line\n", encoding="utf-8")
    assert log_file.stat().st_size > 1024 * 1024
    monkeypatch.setenv("OPENCODE_LOG_FILE", str(log_file))
    manager = OpenCodeProcessManager(Settings.from_env(), _FakeClient([]))

    tail = manager.log_tail(3)
    lines = tail.splitlines()
    assert lines[-1] == "last-line"
    assert len(lines) == 3
    # Bounded: never more than the trailing window is decoded.
    assert len(tail) < 1024 * 1024


class _StubRequest:
    def __init__(self, app, query) -> None:
        self.app = app
        self.query = query


@pytest.mark.asyncio
async def test_log_tail_endpoint_work_is_bounded_by_the_preview_not_the_ring(monkeypatch, tmp_path):
    """The handler previews what it returns, so the request must not pay for a
    full redaction pass over the whole ring (~1.3MB) to then throw ~98% away."""
    from aiohttp import web

    from efp_opencode_adapter.app_keys import OPENCODE_PROCESS_MANAGER_KEY, SETTINGS_KEY
    from efp_opencode_adapter.server import internal_opencode_log_tail_handler

    _clear_adapter_secret_env(monkeypatch)
    monkeypatch.setenv("OPENCODE_LOG_FILE", str(tmp_path / "opencode-serve.log"))
    settings = Settings.from_env()
    manager = OpenCodeProcessManager(settings, _FakeClient([]))
    for index in range(2000):
        manager._log_ring.append(_opencode_debug_line(index).decode("utf-8").rstrip("\n"))
    # A line buffered *before* the adapter knew the secret: re-sanitizing the
    # previewed text on read is exactly what must keep working.
    manager._log_ring.append("LAST-RING-LINE upstream rejected LateLearnedProfileSecret")
    manager._effective_start_env({"EFP_JIRA_0_API_TOKEN": "LateLearnedProfileSecret"})
    ring_chars = sum(len(entry) + 1 for entry in manager._log_ring)
    assert ring_chars > 1_000_000

    sanitized_chars: list[int] = []
    real_sanitize = manager._sanitize

    def counting_sanitize(text: str) -> str:
        sanitized_chars.append(len(text))
        return real_sanitize(text)

    monkeypatch.setattr(manager, "_sanitize", counting_sanitize)

    app = web.Application()
    app[SETTINGS_KEY] = settings
    app[OPENCODE_PROCESS_MANAGER_KEY] = manager
    response = await internal_opencode_log_tail_handler(_StubRequest(app, {"lines": "2000"}))
    body = json.loads(response.text)

    assert sum(sanitized_chars) <= 2 * LOG_TAIL_PREVIEW_CHARS, (
        f"redacted {sum(sanitized_chars)} chars for a {LOG_TAIL_PREVIEW_CHARS}-char preview"
    )
    # Still a useful, still-redacted tail.
    assert "LAST-RING-LINE" in body["log_tail"]
    assert "LateLearnedProfileSecret" not in body["log_tail"]
    assert "***REDACTED***" in body["log_tail"]
    assert len(body["log_tail"].splitlines()) > 1


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


class TestSecretKeyClassification:
    """Redaction must catch credentials without eating LLM telemetry.

    An earlier revision anchored the secret word to the last `_`/`-`/`.`
    component, which silently leaked every camelCase and glued-prefix form
    (`accessToken`, `PGPASSWORD`, `password_hash`) straight to pod stdout.
    """

    @pytest.mark.parametrize(
        "key",
        [
            "token", "api_token", "access_token", "authorization", "apiKey",
            "accessToken", "apiToken", "authToken", "refreshToken", "_authToken",
            "secretKey", "SECRET_KEY", "DJANGO_SECRET_KEY", "CLIENT_SECRET_KEY",
            "PGPASSWORD", "token_value", "password_hash",
        ],
    )
    def test_credential_keys_are_secret(self, key):
        assert opencode_process._is_secret_key(key) is True

    @pytest.mark.parametrize(
        "key",
        ["tokens", "input_tokens", "output_tokens", "max_tokens",
         "tokenizer", "token_count", "token_limit"],
    )
    def test_token_accounting_keys_are_not_secret(self, key):
        assert opencode_process._is_secret_key(key) is False

    @pytest.mark.parametrize(
        "line",
        [
            "INFO session=ses_1 tokens=1523 input_tokens=900 output_tokens=623",
            "INFO tokenizer=cl100k_base token_count=42",
            "INFO secret=false debug=true",
            'usage {"tokens":{"input":10,"output":20}}',
        ],
    )
    def test_telemetry_lines_survive_verbatim(self, line):
        assert opencode_process._SECRET_KV_RE.sub(opencode_process._redact_kv, line) == line

    @pytest.mark.parametrize(
        "line,leaked",
        [
            ('{"accessToken":"OpaqueCredentialValue"}', "OpaqueCredentialValue"),
            ("login password=12345678 failed", "12345678"),
            ("login password=abc{def} failed", "abc{def}"),
            ("env PGPASSWORD=hunter2supersecret psql", "hunter2supersecret"),
        ],
    )
    def test_credential_values_are_fully_redacted(self, line, leaked):
        redacted = opencode_process._SECRET_KV_RE.sub(opencode_process._redact_kv, line)
        assert leaked not in redacted
        assert opencode_process.REDACTED in redacted
