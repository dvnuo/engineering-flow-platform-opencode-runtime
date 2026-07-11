from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from .opencode_client import OpenCodeClient
from .runtime_env import strip_managed_external_env
from .settings import Settings
from .skill_sync import sync_runtime_skills
from .thinking_events import safe_preview, utc_now_iso


class OpenCodeProcessManager:
    def __init__(
        self,
        settings: Settings,
        client: OpenCodeClient | None = None,
        registry_check: Callable[[Settings, OpenCodeClient], Awaitable[dict]] | None = None,
        event_bus: Any | None = None,
    ):
        self.settings = settings
        self.client = client or OpenCodeClient(settings)
        self.registry_check = registry_check
        self.event_bus = event_bus
        self.process: asyncio.subprocess.Process | None = None
        self.last_restart_reason: str | None = None
        self.last_restart_at: str | None = None
        self.health_ok: bool | None = None
        self.registry_ok: bool = False
        self.registry_status: dict | None = None
        self.last_startup_error: str | None = None
        self.log_path: Path = Path(os.getenv("OPENCODE_LOG_FILE") or (self.settings.adapter_state_dir / "opencode-serve.log"))
        self._stopping = False
        self._last_start_env: dict[str, str] = {}
        self._last_start_env_hash: str | None = None

    def _effective_start_env(self, env: dict[str, str] | None) -> dict[str, str]:
        if env is not None:
            clean_env = {str(k): str(v) for k, v in env.items() if v is not None}
            self._last_start_env = dict(clean_env)
            self._last_start_env_hash = self._managed_env_hash(clean_env)
            return clean_env
        if self._last_start_env:
            return dict(self._last_start_env)
        return {}

    def _managed_env_hash(self, env: dict[str, str]) -> str:
        fingerprint = [(str(key), len(str(value))) for key, value in sorted(env.items())]
        return hashlib.sha256(json.dumps(fingerprint, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()

    async def start(self, env: dict[str, str] | None = None, *, reason: str = "startup") -> dict:
        self._stopping = False
        if self.process and self.process.returncode is None:
            return self.status_snapshot()
        try:
            sync_runtime_skills(self.settings)
        except Exception as exc:
            self.health_ok = False
            self.registry_ok = False
            self.last_startup_error = self._sanitize(str(exc))
            raise
        self.log_path = Path(os.getenv("OPENCODE_LOG_FILE") or self.log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.log_path.open("ab")
        base_env = strip_managed_external_env(os.environ)
        managed_env = self._effective_start_env(env)
        child_env = {**base_env, **managed_env}
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
                self.last_startup_error = self._startup_error_with_log_tail(str(exc), self.log_path)
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
        self._stopping = True
        if not self.process or self.process.returncode is not None:
            return self.status_snapshot()
        self.process.terminate()
        try:
            await asyncio.wait_for(self.process.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            self.process.kill()
            await self.process.wait()
        return self.status_snapshot()

    async def restart(self, *, reason: str = "watchdog") -> dict:
        # Watchdog-only revive: config activation is restart-of-the-pod-only,
        # so a managed restart always reuses the env from the boot-time start.
        await self.stop()
        return await self.start(None, reason=reason)

    async def run_watchdog(self, app=None, interval_seconds: float = 10, health_failures_before_restart: int = 3) -> None:
        consecutive_health_failures = 0
        restart_backoff_until = 0.0
        interval = max(0.001, float(interval_seconds))
        failure_threshold = max(1, int(health_failures_before_restart))
        while True:
            await asyncio.sleep(interval)
            if self._stopping:
                continue
            now = time.monotonic()
            if now < restart_backoff_until:
                continue
            try:
                if self.process is None or self.process.returncode is not None:
                    await self._publish_lifecycle_event(
                        "opencode.process.exited",
                        state="failed",
                        data={"reason": "watchdog_process_exited", "status": self.status_snapshot()},
                    )
                    await self._restart_from_watchdog(reason="watchdog_process_exited")
                    consecutive_health_failures = 0
                    restart_backoff_until = time.monotonic() + interval
                    continue

                health = await self.client.health()
                if bool(health.get("healthy")):
                    self.health_ok = True
                    consecutive_health_failures = 0
                    continue

                self.health_ok = False
                consecutive_health_failures += 1
                await self._publish_lifecycle_event(
                    "opencode.health.failed",
                    state="degraded",
                    data={
                        "reason": "watchdog_health_failed",
                        "consecutive_failures": consecutive_health_failures,
                        "threshold": failure_threshold,
                        "health": safe_preview(health, 1000),
                    },
                )
                if consecutive_health_failures >= failure_threshold:
                    await self._restart_from_watchdog(reason="watchdog_health_failed")
                    consecutive_health_failures = 0
                    restart_backoff_until = time.monotonic() + interval
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._publish_lifecycle_event(
                    "opencode.process.restart_failed",
                    state="failed",
                    data={"reason": "watchdog_error", "error": self._sanitize(str(exc)), "status": self.status_snapshot()},
                )
                restart_backoff_until = time.monotonic() + min(60.0, max(1.0, interval) * 2)

    async def _restart_from_watchdog(self, *, reason: str) -> None:
        try:
            status = await self.restart(reason=reason)
        except Exception as exc:
            await self._publish_lifecycle_event(
                "opencode.process.restart_failed",
                state="failed",
                data={"reason": reason, "error": self._sanitize(str(exc)), "status": self.status_snapshot()},
            )
            raise
        await self._publish_lifecycle_event(
            "opencode.process.restarted",
            state="running",
            data={"reason": reason, "status": status},
        )

    async def _publish_lifecycle_event(self, event_type: str, *, state: str, data: dict[str, Any]) -> None:
        bus = self.event_bus
        if bus is None:
            return
        event = {
            "type": event_type,
            "event_type": event_type,
            "engine": "opencode",
            "state": state,
            "summary": event_type,
            "data": safe_preview(data, 4000),
            "created_at": utc_now_iso(),
            "ts": time.time(),
        }
        await bus.publish(event)

    def log_tail(self, lines: int = 200) -> str:
        line_count = max(1, min(int(lines), 2000))
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return ""
        tail = "\n".join(text.splitlines()[-line_count:])
        return self._sanitize(tail)

    def status_snapshot(self) -> dict:
        running = bool(self.process and self.process.returncode is None)
        return {
            "running": running,
            "pid": self.process.pid if self.process else None,
            "returncode": self.process.returncode if self.process else None,
            "health_ok": self.health_ok,
            "registry_ok": self.registry_ok,
            "registry_status": self.registry_status,
            "last_startup_error": self.last_startup_error,
            "last_restart_reason": self.last_restart_reason,
            "last_restart_at": self.last_restart_at,
            "stopping": self._stopping,
            "managed_env_cached": bool(self._last_start_env),
            "managed_env_keys": sorted(self._last_start_env),
            "managed_env_hash": self._last_start_env_hash,
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
