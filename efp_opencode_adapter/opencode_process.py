from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from .opencode_client import OpenCodeClient
from .runtime_env import strip_managed_external_env
from .settings import Settings


class OpenCodeProcessManager:
    def __init__(
        self,
        settings: Settings,
        client: OpenCodeClient | None = None,
        registry_check: Callable[[Settings, OpenCodeClient], Awaitable[dict]] | None = None,
    ):
        self.settings = settings
        self.client = client or OpenCodeClient(settings)
        self.registry_check = registry_check
        self.process: asyncio.subprocess.Process | None = None
        self.last_restart_reason: str | None = None
        self.last_restart_at: str | None = None
        self.health_ok: bool | None = None
        self.registry_ok: bool = False
        self.registry_status: dict | None = None
        self.last_startup_error: str | None = None

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
                cwd=str(self.settings.workspace_dir),
            )
        finally:
            handle.close()
        self.last_restart_reason = reason
        self.last_restart_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self.last_startup_error = None
        self.health_ok = None
        try:
            await self.client.wait_until_ready(self.settings.ready_timeout_seconds)
            self.health_ok = True
        except Exception as exc:
            self.health_ok = False
            self.registry_ok = False
            if not self.last_startup_error:
                self.last_startup_error = self._startup_error_with_log_tail(str(exc), log_path)
            raise
        if self.registry_check:
            registry_status = await self.registry_check(self.settings, self.client)
            self.registry_status = registry_status
            if isinstance(registry_status, dict) and str(registry_status.get("status") or "").lower() == "ok":
                self.registry_ok = True
            else:
                self.health_ok = False
                self.registry_ok = False
                error = self._sanitize(str((registry_status or {}).get("error") if isinstance(registry_status, dict) else "registry failed") or "registry failed")
                self.last_startup_error = error
                raise RuntimeError(error)
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
            "registry_ok": self.registry_ok,
            "registry_status": self.registry_status,
            "last_startup_error": self.last_startup_error,
            "last_restart_reason": self.last_restart_reason,
            "last_restart_at": self.last_restart_at,
        }

    def _startup_error_with_log_tail(self, message: str, log_path: Path) -> str:
        message = self._sanitize(message)
        tail = ""
        try:
            lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()[-200:]
            tail = self._sanitize("\n".join(lines))
        except Exception:
            tail = ""
        return f"{message}; opencode_log_file={log_path}; tail_200={tail}"

    def _sanitize(self, text: str) -> str:
        cleaned = str(text or "")
        for key in ("PORTAL_INTERNAL_TOKEN", "OPENAI_API_KEY", "GITHUB_TOKEN"):
            secret = os.getenv(key, "")
            if secret:
                cleaned = cleaned.replace(secret, "***REDACTED***")
        for prefix in ("ghu_", "gho_", "sk-"):
            idx = cleaned.find(prefix)
            while idx != -1:
                end = idx + len(prefix)
                while end < len(cleaned) and (cleaned[end].isalnum() or cleaned[end] in "_-"):
                    end += 1
                cleaned = cleaned[:idx] + f"{prefix}***REDACTED***" + cleaned[end:]
                idx = cleaned.find(prefix, idx + len(prefix))
        return cleaned
