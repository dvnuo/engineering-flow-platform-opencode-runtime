from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path

from .opencode_client import OpenCodeClient
from .runtime_env import strip_managed_external_env
from .settings import Settings


class OpenCodeProcessManager:
    def __init__(self, settings: Settings, client: OpenCodeClient | None = None):
        self.settings = settings
        self.client = client or OpenCodeClient(settings)
        self.process: asyncio.subprocess.Process | None = None
        self.last_restart_reason: str | None = None
        self.last_restart_at: str | None = None
        self.health_ok: bool | None = None

    async def start(self, env: dict[str, str] | None = None, *, reason: str = "startup") -> dict:
        if self.process and self.process.returncode is None:
            return self.status_snapshot()
        log_path = Path(os.getenv("OPENCODE_LOG_FILE") or (self.settings.adapter_state_dir / "opencode-serve.log"))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = log_path.open("ab")
        base_env = strip_managed_external_env(os.environ)
        child_env = {**base_env, **(env or {})}
        try:
            self.process = await asyncio.create_subprocess_exec(
                "opencode", "serve", "--hostname", "127.0.0.1", "--port", "4096",
                env=child_env,
                stdout=handle,
                stderr=handle,
            )
        finally:
            handle.close()
        self.last_restart_reason = reason
        self.last_restart_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            await self.client.wait_until_ready(self.settings.ready_timeout_seconds)
            self.health_ok = True
        except Exception:
            self.health_ok = False
        return self.status_snapshot()

    async def stop(self, timeout_seconds: float = 10.0) -> dict:
        if not self.process or self.process.returncode is not None:
            return self.status_snapshot()
        self.process.terminate()
        try:
            await asyncio.wait_for(self.process.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            self.process.kill()
            await self.process.wait()
        return self.status_snapshot()

    async def restart(self, env: dict[str, str] | None = None, *, reason: str = "runtime_profile_apply") -> dict:
        await self.stop()
        return await self.start(env, reason=reason)

    def status_snapshot(self) -> dict:
        running = bool(self.process and self.process.returncode is None)
        return {
            "running": running,
            "pid": self.process.pid if self.process else None,
            "health_ok": self.health_ok,
            "last_restart_reason": self.last_restart_reason,
            "last_restart_at": self.last_restart_at,
        }
