from __future__ import annotations

import json
import re
from collections import OrderedDict
import time
from pathlib import Path
from typing import Callable

from .thinking_events import safe_preview, utc_now_iso

# OpenCode streams hundreds of runtime events per turn and the event bridge
# appends every one of them. Re-reading, re-parsing and rewriting the whole
# session file for each append made the per-event cost grow with everything the
# turn had already accumulated (quadratic, on the PVC, on the event loop), so
# the payload is kept in memory and event appends are flushed to disk at most
# once per interval. Entry lifecycle writes (start/finish/fail) still hit disk
# immediately, so nothing that reports run status ever lags.
EVENT_FLUSH_INTERVAL_SECONDS = 2.0
# Chatlogs are megabytes each and the adapter outlives every session it
# serves, so the in-memory cache is bounded (LRU, unflushed entries pinned).
MAX_CACHED_SESSIONS = 32


class ChatLogStore:
    def __init__(
        self,
        chatlogs_dir: Path,
        *,
        event_flush_interval_seconds: float = EVENT_FLUSH_INTERVAL_SECONDS,
        max_cached_sessions: int = MAX_CACHED_SESSIONS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.chatlogs_dir = chatlogs_dir
        self.event_flush_interval_seconds = float(event_flush_interval_seconds)
        self.max_cached_sessions = max(1, int(max_cached_sessions))
        self._clock = clock
        # Insertion-ordered so the least-recently-used entry is evictable: the
        # adapter is a long-lived process that touches every session a pod ever
        # serves, and a chatlog is megabytes, so an unbounded cache is a slow
        # memory leak. Entries with unflushed writes are never evicted.
        self._cache: "OrderedDict[str, dict]" = OrderedDict()
        self._pending: set[str] = set()
        self._last_write_at: dict[str, float] = {}

    def _remember(self, name: str, payload: dict) -> None:
        """Cache ``payload`` as most-recently-used and evict past the cap."""

        self._cache[name] = payload
        self._cache.move_to_end(name)
        # Never evict the payload being remembered. When every older entry is
        # pending this may temporarily exceed the cap, but the current payload
        # is about to become pending too; evicting it here can leave a pending
        # name without its payload and lose coalesced events.
        for candidate in [
            key
            for key in self._cache
            if key != name and key not in self._pending
        ][: max(0, len(self._cache) - self.max_cached_sessions)]:
            self._cache.pop(candidate, None)
            self._last_write_at.pop(candidate, None)

    def _sanitize(self, session_id: str) -> str:
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", str(session_id)).strip(".-")
        return name or "default"

    def _path(self, session_id: str) -> Path:
        return self.chatlogs_dir / f"{self._sanitize(session_id)}.json"

    def get(self, session_id: str) -> dict | None:
        name = self._sanitize(session_id)
        cached = self._cache.get(name)
        if cached is not None:
            self._cache.move_to_end(name)
            return cached
        p = self._path(session_id)
        if not p.exists():
            return None
        payload = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            self._remember(name, payload)
        return payload

    def _write(self, session_id: str, payload: dict) -> dict:
        name = self._sanitize(session_id)
        self._remember(name, payload)
        self.chatlogs_dir.mkdir(parents=True, exist_ok=True)
        path = self._path(session_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        self._pending.discard(name)
        self._last_write_at[name] = self._clock()
        return payload

    def _write_coalesced(self, session_id: str, payload: dict) -> dict:
        """Persist an event append at most once per flush interval.

        Only the trip to the PVC is deferred: readers go through the in-memory
        payload, which stays exact, and the next lifecycle write, the next
        append past the interval, or shutdown lands it on disk.
        """
        name = self._sanitize(session_id)
        self._remember(name, payload)
        last_write_at = self._last_write_at.get(name)
        if last_write_at is not None and (self._clock() - last_write_at) < self.event_flush_interval_seconds:
            self._pending.add(name)
            return payload
        return self._write(session_id, payload)

    def flush(self, session_id: str) -> bool:
        """Write out coalesced event appends. Returns True if anything moved."""
        name = self._sanitize(session_id)
        payload = self._cache.get(name)
        if name not in self._pending or payload is None:
            self._pending.discard(name)
            return False
        self._write(session_id, payload)
        return True

    def flush_all(self) -> int:
        return sum(1 for name in list(self._pending) if self.flush(name))

    def _forget(self, session_id: str) -> None:
        name = self._sanitize(session_id)
        self._cache.pop(name, None)
        self._pending.discard(name)
        self._last_write_at.pop(name, None)

    def _fresh_payload(self, session_id: str) -> dict:
        return {
            "session_id": session_id,
            "engine": "opencode",
            "entries": [],
            "updated_at": utc_now_iso(),
        }

    def _quarantine_corrupt_file(self, session_id: str) -> Path | None:
        self._forget(session_id)
        path = self._path(session_id)
        if not path.exists():
            return None

        stamp = utc_now_iso().replace(":", "").replace("+", "_").replace("/", "_")
        backup = path.with_name(f"{path.name}.corrupt-{stamp}")

        try:
            path.replace(backup)
            return backup
        except Exception:
            return None

    def _ensure(self, session_id: str) -> dict:
        try:
            cur = self.get(session_id)
        except Exception:
            self._quarantine_corrupt_file(session_id)
            return self._fresh_payload(session_id)

        if cur:
            return cur

        return self._fresh_payload(session_id)

    def _find_entry(self, payload: dict, request_id: str) -> dict | None:
        entries = payload.setdefault("entries", [])
        for entry in reversed(entries):
            if entry.get("request_id") == request_id:
                return entry
        return None

    def _new_entry(self, request_id: str) -> dict:
        return {
            "request_id": request_id,
            "status": "running",
            "message": "",
            "response": "",
            "events": [],
            "runtime_events": [],
            "context_state": {},
            "llm_debug": {},
            "created_at": utc_now_iso(),
            "finished_at": "",
        }

    def start_entry(self, session_id: str, *, request_id: str, message: str, runtime_events: list[dict] | None = None, context_state: dict | None = None, llm_debug: dict | None = None) -> dict:
        d = self._ensure(session_id)
        d.setdefault("entries", []).append({
            "request_id": request_id,
            "status": "running",
            "message": safe_preview(message, 2000),
            "response": "",
            "events": [],
            "runtime_events": safe_preview(runtime_events or [], 4000),
            "context_state": safe_preview(context_state or {}, 4000),
            "llm_debug": safe_preview(llm_debug or {}, 4000),
            "created_at": utc_now_iso(),
            "finished_at": "",
        })
        d["updated_at"] = utc_now_iso()
        return self._write(session_id, d)

    def finish_entry(self, session_id: str, *, request_id: str, status: str, response: str, runtime_events: list[dict] | None = None, events: list[dict] | None = None, context_state: dict | None = None, llm_debug: dict | None = None) -> dict:
        d = self._ensure(session_id)
        e = self._find_entry(d, request_id)
        if e is None:
            e = self._new_entry(request_id)
            d.setdefault("entries", []).append(e)

        e.update({
            "status": status,
            "response": safe_preview(response, 2000),
            "runtime_events": safe_preview(runtime_events if runtime_events is not None else e.get("runtime_events", []), 4000),
            "events": safe_preview(events if events is not None else e.get("events", []), 4000),
            "context_state": safe_preview(context_state if context_state is not None else e.get("context_state", {}), 4000),
            "llm_debug": safe_preview(llm_debug if llm_debug is not None else e.get("llm_debug", {}), 4000),
            "finished_at": utc_now_iso(),
        })
        d["updated_at"] = utc_now_iso()
        return self._write(session_id, d)

    def fail_entry(self, session_id: str, *, request_id: str, error: str, runtime_events: list[dict] | None = None, context_state: dict | None = None, llm_debug: dict | None = None) -> dict:
        return self.finish_entry(session_id, request_id=request_id, status="error", response=error, runtime_events=runtime_events, events=runtime_events, context_state=context_state, llm_debug=llm_debug)

    def append_event(self, session_id: str, *, request_id: str, event: dict, runtime: bool = True) -> dict:
        d = self._ensure(session_id)
        e = self._find_entry(d, request_id)
        if e is None:
            e = self._new_entry(request_id)
            d.setdefault("entries", []).append(e)
        key = "runtime_events" if runtime else "events"
        e.setdefault(key, []).append(safe_preview(event, 1000))
        d["updated_at"] = utc_now_iso()
        return self._write_coalesced(session_id, d)

    def latest_entry(self, session_id: str) -> dict | None:
        d = self.get(session_id)
        if not d or not d.get("entries"):
            return None
        return d["entries"][-1]

    def list_session_ids(self) -> list[str]:
        out = []
        for p in self.chatlogs_dir.glob("*.json"):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")).get("session_id", p.stem))
            except Exception:
                out.append(p.stem)
        return sorted(set(out))

    def delete(self, session_id: str) -> bool:
        self._forget(session_id)
        path = self._path(session_id)
        if not path.exists():
            return False
        path.unlink()
        return True
