from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from .opencode_client import OpenCodeClient
from .runtime_env import SECRET_MARKERS, strip_managed_external_env
from .settings import Settings
from .skill_sync import sync_runtime_skills
from .thinking_events import safe_preview, utc_now_iso

logger = logging.getLogger(__name__)

# The child's output is pumped through the adapter so it reaches `kubectl logs`.
# The ring buffer serves log_tail() without re-reading the PVC-backed file, and
# the file handle is block-buffered so the pump never waits on a network write
# per line.
CHILD_LOG_RING_LINES = 2000
CHILD_LOG_FILE_BUFFER_BYTES = 64 * 1024
# asyncio's default StreamReader limit is 64KB; opencode emits long JSON debug
# lines, and anything above the limit is dropped with a warning.
CHILD_LOG_STREAM_LIMIT_BYTES = 1024 * 1024
CHILD_LOG_FLUSH_INTERVAL_SECONDS = 2.0
# Draining is progress-based rather than a fixed wall-clock budget: a child
# that dumps a stack trace and dies out-runs the pump, and a fixed budget
# discards exactly the burst tail (the fatal lines) this forwarding exists to
# surface. The pump is only abandoned once it has made no progress for the idle
# window, with a generous ceiling so a genuinely wedged reader cannot block
# shutdown forever - and a much tighter one once the child itself is gone (see
# CHILD_LOG_PUMP_DEAD_CHILD_MAX_SECONDS).
CHILD_LOG_PUMP_IDLE_SECONDS = 2.0
CHILD_LOG_PUMP_MAX_DRAIN_SECONDS = 120.0
# Once the child is dead its own output is whatever the OS pipe still holds -
# bounded by the pipe buffer and drained in milliseconds. Anything still
# *arriving* is a tool subprocess that inherited the merged pipe and outlived
# opencode; its progress must not keep stop()/restart() open for the ceiling
# above while opencode is down and every request 502s.
CHILD_LOG_PUMP_DEAD_CHILD_MAX_SECONDS = 3.0
CHILD_LOG_PUMP_RESTART_MAX_DRAIN_SECONDS = 5.0
CHILD_LOG_PUMP_SETTLE_SECONDS = 0.25
# readline() does not suspend while the pipe buffer is non-empty and _sanitize
# is pure CPU, so a burst (npm test / mvn verify relayed through opencode) would
# otherwise hold the loop for seconds at a time and stall /health, /ready, SSE
# delivery and the watchdog's own probe. Yield on whichever bound trips first;
# both are far below one scheduling quantum's worth of work.
CHILD_LOG_PUMP_YIELD_EVERY_LINES = 32
CHILD_LOG_PUMP_YIELD_INTERVAL_SECONDS = 0.005
LOG_TAIL_MAX_BYTES = 1024 * 1024
# The log-tail endpoint previews what it returns; sanitising more than the
# preview is blocking event-loop CPU that is thrown away (a full ring is ~1.3MB
# of which the response keeps ~1.5%).
LOG_TAIL_PREVIEW_CHARS = 20000

REDACTED = "***REDACTED***"
# Secrets reach the child through the profile projection env, not the adapter's
# own os.environ (runtime_env strips MANAGED_EXTERNAL_ENV_KEYS); these three are
# the only ones the adapter process itself still holds.
ADAPTER_OWN_SECRET_ENV_KEYS = ("PORTAL_INTERNAL_TOKEN", "OPENAI_API_KEY", "GITHUB_TOKEN")
# Short values would shred ordinary log text without protecting anything.
MIN_REDACTED_SECRET_LENGTH = 6
AWS_SECRET_ACCESS_KEY_LENGTH = 40

# Credential *shapes*, matched in one pass (this runs per child line, so the
# alternation is deliberately a single scan rather than one regex per family).
# Each branch keeps its family prefix so the line stays diagnosable.
_SHAPE_GROUP_NAMES = ("url", "ghpat", "gh", "sk", "atlassian", "awsid", "scheme")
_SECRET_SHAPE_RE = re.compile(
    # Credentials embedded in proxy/clone URLs: HTTP_PROXY carries the proxy
    # password and git echoes remote URLs on failure.
    r"\b(?P<url>[A-Za-z][A-Za-z0-9+.\-]*://)[^/\s?#@]+@"
    # GitHub fine-grained PATs, then classic PAT / OAuth / user / server / refresh.
    r"|\b(?P<ghpat>github_pat_)[A-Za-z0-9_]{6,}"
    r"|\b(?P<gh>gh[pousr]_)[A-Za-z0-9_\-]{6,}"
    r"|\b(?P<sk>sk-)[A-Za-z0-9_\-]{8,}"
    # Atlassian API tokens: ATATT (account) / ATCTT (scoped).
    r"|\b(?P<atlassian>AT[AC]TT)[A-Za-z0-9_\-=+/.]{8,}"
    # AWS access key ids.
    r"|\b(?P<awsid>AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b"
    r"|\b(?P<scheme>(?i:bearer|basic)\s+)[A-Za-z0-9_\-.=+/]{8,}"
)
# AWS secret access keys: 40 chars of the base64 alphabet and no delimiter of
# their own. Mixed case + digits are required in the replacement so hex digests,
# git object ids and ordinary words survive. Kept as its own single-branch
# pattern: folding it into the alternation above defeats the engine's leading
# character-set optimisation and doubles the per-line cost.
_AWS_SECRET_SHAPE_RE = re.compile(
    r"(?<![A-Za-z0-9/+=])[A-Za-z0-9/+=]{%d}(?![A-Za-z0-9/+=])" % AWS_SECRET_ACCESS_KEY_LENGTH
)
# `password=...`, `api_key: ...`, `authorization=...`, `aws_secret_access_key=...`
# The key is matched as a bounded token anchored at a token start and classified
# in the replacement, so the pattern stays linear on the long JSON lines
# opencode emits.
_SECRET_KV_RE = re.compile(
    r"(?<![A-Za-z0-9_.\-])([A-Za-z0-9_.\-]{1,64})(?![A-Za-z0-9_.\-])"
    # The optional quote picks up JSON log lines: {"api_token": "..."}.
    r"([\"']?\s*[:=]\s*)"
    # A value that *starts* with `{`/`[` is a nested JSON structure, not a
    # credential: consuming it turned `"tokens":{"input":10}` into unparseable
    # text, so the bare branch declines to match it at all. A value that merely
    # *contains* a brace still matches in full — stopping at the brace redacted
    # only `abc` of `password=abc{def}` and leaked the tail.
    r"(\"[^\"]*\"|'[^']*'|(?![{\[])[^\s,;&\"']+)"
)
# The secret key *words*. A key is only treated as a credential when one of
# these is the whole key or its last `_`/`-`/`.`-separated component, so
# `token`, `api_token` and `access_token` are still caught while the
# token-accounting keys an LLM runtime emits on nearly every line (`tokens`,
# `input_tokens`, `output_tokens`, `tokenizer`, `token_count`) keep their
# values. Substring matching redacted all of those and gutted the very
# observability this forwarding exists to provide.
_SECRET_KEY_WORDS = (
    r"passwd|password|secret|token|api[_.\-]?key|apikey|access[_.\-]?key"
    r"|credentials?|authorization|auth[_.\-]?key"
)
# Cheap line-level gate: the key/value sweep is skipped entirely unless one of
# the words occurs somewhere in the line.
_SECRET_KEY_HINT_RE = re.compile(r"(?i)%s" % _SECRET_KEY_WORDS)
# Words that are unambiguous enough to match anywhere inside the key. No
# telemetry key contains them, and they appear glued to a prefix constantly:
# ``PGPASSWORD``, ``DJANGO_SECRET_KEY``, ``password_hash``, ``secretKey``.
# Anchoring these to the last `_`/`-`/`.` component (as an earlier revision
# did) leaked every one of those to pod stdout.
_STRONG_SECRET_KEY_RE = re.compile(
    r"(?i)(?:passwd|password|secret|credentials?|authorization"
    r"|api[_.\-]?key|apikey|access[_.\-]?key|auth[_.\-]?key)"
)
# ``token`` is the ambiguous one: singular is a credential, plural and the
# counter/encoder forms are the telemetry an LLM runtime prints on nearly
# every line. So it is matched as a whole *component* only, after splitting
# camelCase as well as `_`/`-`/`.` — camelCase is the dominant style in the
# TypeScript runtime whose output this pump forwards (``accessToken``,
# ``authToken``, ``_authToken`` from npm/yarn).
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_KEY_SPLIT_RE = re.compile(r"[_.\-]+")
_TOKEN_TELEMETRY_COMPONENTS = frozenset({"tokens", "tokenizer", "tokenized"})
_TOKEN_TELEMETRY_NEIGHBOURS = frozenset({"count", "limit", "usage", "budget", "size"})
# Values that cannot be a credential. Booleans/nulls keep `secret=false`
# readable. Numbers are deliberately NOT exempt: a numeric password or PIN
# (`password=12345678`) is a credential, and the token-accounting keys that
# motivated the exemption are already protected by the *key* test above.
_NON_SECRET_VALUE_RE = re.compile(r"(?i)\A(?:true|false|null|none|nil|undefined)\Z")


def _is_secret_key(key: str) -> bool:
    """True when ``key`` names a credential rather than telemetry."""

    if _STRONG_SECRET_KEY_RE.search(key):
        return True
    components = [
        part.lower()
        for part in _KEY_SPLIT_RE.split(_CAMEL_BOUNDARY_RE.sub("_", key))
        if part
    ]
    if not components:
        return False
    if _TOKEN_TELEMETRY_COMPONENTS.intersection(components):
        return False
    if "token" not in components:
        return False
    # `token_count`, `token_limit`, ... are counters, not credentials.
    return not _TOKEN_TELEMETRY_NEIGHBOURS.intersection(components)


def _is_secret_env_key(key: str) -> bool:
    return any(marker in str(key).upper() for marker in SECRET_MARKERS)


def _redact_kv(match: "re.Match[str]") -> str:
    # `input_tokens`/`tokenizer` are telemetry; `token`, `accessToken` and
    # `PGPASSWORD` are credentials. See _is_secret_key.
    if not _is_secret_key(match.group(1)):
        return match.group(0)
    value = match.group(3)
    # An earlier pass already replaced the interesting part; keep its shape hint.
    if REDACTED in value:
        return match.group(0)
    quote = value[0] if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0] else ""
    bare = value[1:-1].strip() if quote else value.strip()
    if not bare or _NON_SECRET_VALUE_RE.match(bare):
        return match.group(0)
    # Keep the quotes so a redacted JSON log line is still a JSON log line.
    return f"{match.group(1)}{match.group(2)}{quote}{REDACTED}{quote}"


def _redact_shape(match: "re.Match[str]") -> str:
    for name in _SHAPE_GROUP_NAMES:
        prefix = match.group(name)
        if prefix is not None:
            return f"{prefix}{REDACTED}@" if name == "url" else f"{prefix}{REDACTED}"
    return REDACTED


def _redact_aws_secret_shape(match: "re.Match[str]") -> str:
    candidate = match.group(0)
    if not (
        any(char.islower() for char in candidate)
        and any(char.isupper() for char in candidate)
        and any(char.isdigit() for char in candidate)
    ):
        return candidate
    return REDACTED


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
        self._log_ring: deque[str] = deque(maxlen=CHILD_LOG_RING_LINES)
        self._output_task: asyncio.Task | None = None
        self._output_progress = 0
        self._secret_values: tuple[str, ...] = ()
        self._refresh_secret_values()

    def _effective_start_env(self, env: dict[str, str] | None) -> dict[str, str]:
        if env is not None:
            clean_env = {str(k): str(v) for k, v in env.items() if v is not None}
            self._last_start_env = dict(clean_env)
            self._last_start_env_hash = self._managed_env_hash(clean_env)
            self._refresh_secret_values()
            return clean_env
        if self._last_start_env:
            return dict(self._last_start_env)
        return {}

    def _refresh_secret_values(self) -> None:
        """Snapshot the values that must never reach stdout.

        The child's secrets live in the projection env handed to spawn, not in
        the adapter's os.environ, so redacting against os.environ alone leaves
        every profile credential in the cluster log pipeline.
        """
        values = {os.getenv(key, "") for key in ADAPTER_OWN_SECRET_ENV_KEYS}
        for key, value in self._last_start_env.items():
            if _is_secret_env_key(key):
                values.add(str(value or ""))
        self._secret_values = tuple(
            sorted((value for value in values if len(value) >= MIN_REDACTED_SECRET_LENGTH), key=len, reverse=True)
        )

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
        # A previous child's pump (e.g. a start() after a bare crash) must be
        # retired before a new one takes over the log file.
        await self._drain_output_pump()
        handle = self.log_path.open("ab", buffering=CHILD_LOG_FILE_BUFFER_BYTES)
        base_env = strip_managed_external_env(os.environ)
        managed_env = self._effective_start_env(env)
        child_env = {**base_env, **managed_env}
        spawned = False
        try:
            # Merged pipe (not the log file directly) so the adapter can re-emit
            # every child line on its own stdout; the file is still written by
            # the pump below.
            self.process = await asyncio.create_subprocess_exec(
                "opencode", "serve", "--hostname", "127.0.0.1", "--port", "4096",
                env=child_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                limit=CHILD_LOG_STREAM_LIMIT_BYTES,
                cwd=str(self.settings.workspace_dir),
            )
            spawned = True
        finally:
            if not spawned:
                handle.close()
        self._start_output_pump(self.process, handle)
        logger.info(
            "opencode.process.started pid=%s reason=%s log_file=%s",
            getattr(self.process, "pid", None),
            reason,
            self.log_path,
        )
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
                await self._settle_output_pump()
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

    def _start_output_pump(self, process: Any, handle: Any) -> None:
        """Fan the child's merged stdout/stderr onto the adapter's stdout.

        A process without a readable stream (injected/fake) keeps the previous
        behaviour: the log file handle is simply released.
        """
        stream = getattr(process, "stdout", None)
        if stream is None or not hasattr(stream, "readline"):
            handle.close()
            self._output_task = None
            return
        self._output_task = asyncio.create_task(self._pump_child_output(process, stream, handle))

    async def _pump_child_output(self, process: Any, stream: Any, handle: Any) -> None:
        pid = getattr(process, "pid", None)
        last_flush = time.monotonic()
        last_yield = last_flush
        lines_since_yield = 0
        try:
            while True:
                # Fairness first, so it also covers the over-long-line path
                # below: sleep(0) reschedules this task behind every callback
                # already queued on the loop, at ~1us a time.
                lines_since_yield += 1
                tick = time.monotonic()
                if (
                    lines_since_yield >= CHILD_LOG_PUMP_YIELD_EVERY_LINES
                    or (tick - last_yield) >= CHILD_LOG_PUMP_YIELD_INTERVAL_SECONDS
                ):
                    lines_since_yield = 0
                    last_yield = tick
                    await asyncio.sleep(0)
                try:
                    raw = await stream.readline()
                except ValueError:
                    # Line longer than the StreamReader limit; asyncio already
                    # dropped it from the buffer, keep pumping. Still progress:
                    # a drain must not treat a run of over-long lines as stalled.
                    self._output_progress += 1
                    logger.warning("opencode.log.line_dropped pid=%s reason=line_too_long", pid)
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("opencode.log.read_failed pid=%s error=%s", pid, self._sanitize(str(exc)))
                    break
                if not raw:
                    break
                self._output_progress += 1
                text = self._sanitize(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
                if text:
                    self._log_ring.append(text)
                    logger.info("opencode: %s", text)
                # Inline: the handle is block-buffered, so this is a memcpy into
                # the 64KB buffer. Handing every line to the executor instead
                # costs ~276us, which is far slower than opencode can emit —
                # the OS pipe then fills and the child blocks in write(), i.e.
                # the adapter becomes a throughput valve on `opencode serve`.
                self._append_child_log(handle, raw)
                now = time.monotonic()
                if (now - last_flush) >= CHILD_LOG_FLUSH_INTERVAL_SECONDS:
                    last_flush = now
                    last_yield = now
                    lines_since_yield = 0
                    # Only the flush touches the network PVC; off-loop that.
                    await asyncio.to_thread(self._flush_child_log, handle)
        finally:
            # close() flushes, so whatever is still in the block buffer lands
            # even when the drain gave up and cancelled us mid-burst.
            self._close_child_log(handle)
            logger.info("opencode.process.output_closed pid=%s returncode=%s", pid, getattr(process, "returncode", None))

    @staticmethod
    def _append_child_log(handle: Any, raw: bytes) -> None:
        try:
            handle.write(raw if raw.endswith(b"\n") else raw + b"\n")
        except Exception:
            # The stdout copy is the one that must survive; a broken log file
            # must never kill the pump or the child.
            pass

    @staticmethod
    def _flush_child_log(handle: Any) -> None:
        try:
            handle.flush()
        except Exception:
            pass

    @staticmethod
    def _close_child_log(handle: Any) -> None:
        try:
            handle.close()
        except Exception:
            pass

    async def _settle_output_pump(self, timeout_seconds: float = CHILD_LOG_PUMP_SETTLE_SECONDS) -> None:
        """Let the pump land already-queued child lines in the ring (without
        cancelling it) so startup diagnostics carry the child's own output."""
        task = self._output_task
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _drain_output_pump(
        self,
        idle_timeout_seconds: float = CHILD_LOG_PUMP_IDLE_SECONDS,
        max_seconds: float = CHILD_LOG_PUMP_MAX_DRAIN_SECONDS,
    ) -> None:
        """Wait for the pump to reach EOF, giving up only once the child's
        output has made no progress for ``idle_timeout_seconds``.

        A fixed wall-clock budget truncated the log exactly where it matters:
        a crashing child out-runs the pump, so the discarded tail is the stack
        trace. Keep waiting while lines are still landing; the ceiling only
        guards against a reader that never returns.

        Once the child itself is gone the progress-based wait is no longer
        safe: `opencode serve` hands the merged stdout pipe to every tool
        subprocess it spawns, and an orphan that outlives it keeps the pipe
        open and keeps writing. That progress is not the crash tail, so a dead
        child gets a hard, short ceiling - long enough to drain the pipe buffer
        the child left behind many times over, short enough that a restart is
        not held for minutes with opencode down.
        """
        task = self._output_task
        if task is None:
            return
        self._output_task = None
        if not self._child_is_running():
            max_seconds = min(max_seconds, CHILD_LOG_PUMP_DEAD_CHILD_MAX_SECONDS)
        deadline = time.monotonic() + max(0.0, max_seconds)
        idle_window = max(0.0, idle_timeout_seconds)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            progress_marker = self._output_progress
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=min(idle_window, remaining))
                return
            except asyncio.TimeoutError:
                if self._output_progress == progress_marker:
                    break
            except asyncio.CancelledError:
                raise
            except Exception:
                return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def _child_is_running(self) -> bool:
        process = self.process
        return process is not None and getattr(process, "returncode", None) is None

    async def stop(
        self,
        timeout_seconds: float = 10.0,
        *,
        drain_max_seconds: float = CHILD_LOG_PUMP_MAX_DRAIN_SECONDS,
    ) -> dict:
        self._stopping = True
        if not self.process or self.process.returncode is not None:
            await self._drain_output_pump(max_seconds=drain_max_seconds)
            return self.status_snapshot()
        self.process.terminate()
        try:
            await asyncio.wait_for(self.process.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            self.process.kill()
            await self.process.wait()
        await self._drain_output_pump(max_seconds=drain_max_seconds)
        return self.status_snapshot()

    async def restart(self, *, reason: str = "watchdog") -> dict:
        # Watchdog-only revive: config activation is restart-of-the-pod-only,
        # so a managed restart always reuses the env from the boot-time start.
        # A restart runs with opencode already down and every request failing,
        # so the drain gets its own cap on top of the dead-child bound.
        await self.stop(drain_max_seconds=CHILD_LOG_PUMP_RESTART_MAX_DRAIN_SECONDS)
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

    def log_tail(self, lines: int = 200, *, max_chars: int = LOG_TAIL_PREVIEW_CHARS) -> str:
        line_count = max(1, min(int(lines), CHILD_LOG_RING_LINES))
        if self._log_ring:
            selected = list(self._log_ring)[-line_count:]
        else:
            selected = self._log_file_tail(line_count)
        # Ring entries were already sanitized on append; this pass exists to
        # re-apply secrets learned *after* a line was buffered, so it must still
        # run - but only over the text the caller actually keeps. Re-running the
        # whole pattern set over the full ring was 280ms of blocking event-loop
        # CPU per log-tail request, ~98% of it thrown away by the preview.
        return self._sanitize("\n".join(self._tail_within_budget(selected, max(1, int(max_chars)))))

    @staticmethod
    def _tail_within_budget(lines: list[str], max_chars: int) -> list[str]:
        """The trailing whole lines that fit in ``max_chars`` (always at least
        one). Whole lines only: slicing mid-line could strip a credential's
        prefix and leave the remainder unrecognisable to the shape patterns."""
        selected: list[str] = []
        total = 0
        for line in reversed(lines):
            total += len(line) + 1
            if selected and total > max_chars:
                break
            selected.append(line)
        selected.reverse()
        return selected

    def _log_file_tail(self, line_count: int) -> list[str]:
        """Bounded tail read: only the last LOG_TAIL_MAX_BYTES are parsed, so a
        multi-GB log file on the PVC can never be pulled into memory."""
        start = 0
        try:
            with self.log_path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                start = max(0, handle.tell() - LOG_TAIL_MAX_BYTES)
                handle.seek(start)
                data = handle.read()
        except Exception:
            return []
        text = data.decode("utf-8", errors="ignore")
        if start > 0:
            # Drop the partial first line produced by the byte-offset seek.
            text = text.split("\n", 1)[1] if "\n" in text else ""
        return text.splitlines()[-line_count:]

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
        # log_tail() prefers the in-memory ring, so the tail is fresh even
        # though the log file itself is block-buffered.
        tail = self.log_tail(200)
        return f"{message}; opencode_log_file={log_path}; tail_200={tail}"

    def _sanitize(self, text: str) -> str:
        """Fail closed: redact known secret values *and* anything shaped like a
        credential before it leaves for stdout / the cluster log pipeline."""
        cleaned = str(text or "")
        if not cleaned:
            return cleaned
        for secret in self._secret_values:
            if secret:
                cleaned = cleaned.replace(secret, REDACTED)
        cleaned = _SECRET_SHAPE_RE.sub(_redact_shape, cleaned)
        cleaned = _AWS_SECRET_SHAPE_RE.sub(_redact_aws_secret_shape, cleaned)
        # Shapes run first so `token=ghp_x` keeps its family hint; the key/value
        # sweep then catches credentials with no recognisable shape at all.
        if _SECRET_KEY_HINT_RE.search(cleaned):
            cleaned = _SECRET_KV_RE.sub(_redact_kv, cleaned)
        return cleaned
