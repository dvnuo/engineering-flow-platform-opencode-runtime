"""What a session is currently blocked on, and who has claimed it.

Portal renders an approval card from a live `permission.requested` event, but a
page refresh or a dropped socket loses that event forever -- and the run stays
blocked with nothing on screen to unblock it. Portal therefore also polls
`/api/sessions/{id}/pending-input`, and this is what answers.

The native runtime keeps the same state in session metadata. OpenCode has no
equivalent: `GET /session/{id}` returns no pending permission, and the only
record of one is the event that announced it. So the adapter keeps its own,
fed from the event bus.

Scope note: OpenCode 1.14.x has a permission API (`POST
/session/{id}/permissions/{permissionID}`) but no question API -- there is no
route to deliver a typed answer back to a `question` tool call. Only permission
requests can be tracked and answered here; `question_request` is always None.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

# A permission request is answered within one turn or abandoned with the run.
# The cap is a leak stop, not a working limit: it is far above the number of
# sessions one runtime holds open at once.
DEFAULT_MAX_SESSIONS = 512

RECORD_EVENT_TYPES = {"permission.requested", "permission_request"}

RESOLVED_EVENT_TYPES = {"permission.resolved", "permission_resolved"}

# Once the run that raised it is over, a pending permission can no longer be
# answered -- OpenCode has already dropped the waiter.
RUN_ENDED_EVENT_TYPES = {"chat.completed", "chat.failed"}

CLEAR_EVENT_TYPES = RESOLVED_EVENT_TYPES | RUN_ENDED_EVENT_TYPES


@dataclass
class PendingPermission:
    request: dict[str, Any]
    permission_id: str
    opencode_session_id: str = ""
    request_id: str = ""
    claimed: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def _event_type(event: dict[str, Any]) -> str:
    return str(event.get("type") or event.get("event_type") or "")


def _legacy_type(event: dict[str, Any]) -> str:
    return str(event.get("legacy_type") or event.get("legacy_event_type") or "")


def _event_session_id(event: dict[str, Any]) -> str:
    value = event.get("session_id")
    if value:
        return str(value)
    data = event.get("data")
    if isinstance(data, dict) and data.get("session_id"):
        return str(data["session_id"])
    return ""


def _event_permission_id(event: dict[str, Any]) -> str:
    value = event.get("permission_id")
    if value:
        return str(value)
    data = event.get("data")
    if isinstance(data, dict):
        for key in ("permission_id", "id"):
            if data.get(key):
                return str(data[key])
    return ""


class PendingInputStore:
    """Latest unanswered permission per portal session."""

    def __init__(self, *, max_sessions: int = DEFAULT_MAX_SESSIONS) -> None:
        self.max_sessions = max(1, int(max_sessions))
        self._pending: OrderedDict[str, PendingPermission] = OrderedDict()

    # ------------------------------------------------------------- observing

    def observe_event(self, event: Any) -> None:
        """Update state from one runtime event. Never raises into the bus."""

        try:
            self._observe_event(event)
        except Exception:
            # Losing a pending card degrades recovery; letting this escape
            # would drop the event for every websocket subscriber.
            pass

    def _observe_event(self, event: Any) -> None:
        if not isinstance(event, dict):
            return
        # A replayed event is history being re-sent to a reconnecting client,
        # not a fresh block; acting on it would resurrect an answered card.
        metadata = event.get("metadata")
        if isinstance(metadata, dict) and metadata.get("replayed"):
            return

        types = {_event_type(event), _legacy_type(event)} - {""}
        session_id = _event_session_id(event)
        if not session_id:
            return

        if types & CLEAR_EVENT_TYPES:
            # A resolution names the permission it resolved, so it can only
            # clear that one. The run simply ending clears whatever is pending.
            resolved_id = _event_permission_id(event) if types & RESOLVED_EVENT_TYPES else None
            self.clear(session_id, permission_id=resolved_id)
            return

        if not (types & RECORD_EVENT_TYPES):
            return

        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        request = data.get("permission_request")
        if not isinstance(request, dict):
            return
        permission_id = str(request.get("permission_id") or request.get("request_id") or "")
        if not permission_id:
            return

        existing = self._pending.get(session_id)
        self._pending[session_id] = PendingPermission(
            request=dict(request),
            permission_id=permission_id,
            opencode_session_id=str(event.get("opencode_session_id") or request.get("opencode_session_id") or ""),
            request_id=str(event.get("request_id") or ""),
            # OpenCode's event is `permission.updated`, and nothing stops it
            # arriving twice for one permission. Re-recording with a fresh
            # claim would release a response already in flight and let the
            # same tool call be approved twice.
            claimed=bool(existing is not None and existing.permission_id == permission_id and existing.claimed),
        )
        self._pending.move_to_end(session_id)
        while len(self._pending) > self.max_sessions:
            self._pending.popitem(last=False)

    # ---------------------------------------------------------------- reading

    def get(self, session_id: str) -> PendingPermission | None:
        return self._pending.get(str(session_id or ""))

    def snapshot(self, session_id: str) -> dict[str, Any]:
        """The pending-input payload Portal polls for."""

        session_id = str(session_id or "")
        entry = self._pending.get(session_id)
        return {
            "session_id": session_id,
            # OpenCode exposes no question-reply route, so a question is never
            # reported as answerable here. See the module docstring.
            "question_request": None,
            "permission_request": dict(entry.request) if entry else None,
            "last_execution_id": entry.request_id if entry else None,
        }

    # --------------------------------------------------------------- claiming

    def claim(self, session_id: str, permission_id: str) -> bool:
        """Take exclusive ownership of one pending request.

        A double-submitted response would otherwise pass the id check twice and
        approve the same tool call twice.
        """
        entry = self._pending.get(str(session_id or ""))
        if entry is None or entry.permission_id != str(permission_id or ""):
            return False
        if entry.claimed:
            return False
        entry.claimed = True
        return True

    def release(self, session_id: str, permission_id: str) -> None:
        entry = self._pending.get(str(session_id or ""))
        if entry is not None and entry.permission_id == str(permission_id or ""):
            entry.claimed = False

    def clear(self, session_id: str, *, permission_id: str | None = None) -> None:
        session_id = str(session_id or "")
        entry = self._pending.get(session_id)
        if entry is None:
            return
        # A resolution names the permission it resolved. Clearing on a
        # mismatch would drop a newer request that is still waiting.
        if permission_id and entry.permission_id != str(permission_id):
            return
        self._pending.pop(session_id, None)
