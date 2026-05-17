from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .thinking_events import utc_now_iso


_SAFE_ATTACHMENT_KEYS = {
    "file_id",
    "id",
    "name",
    "filename",
    "content_type",
    "mime",
    "size",
    "type",
    "parsed",
    "parse_error",
}
_STRING_ATTACHMENT_KEYS = {
    "file_id",
    "id",
    "name",
    "filename",
    "content_type",
    "mime",
    "parse_error",
}
_ALLOWED_ATTACHMENT_TYPES = {"image", "file"}


def _clean_string(value: Any) -> str | None:
    text = str(value).strip()
    return text or None


def sanitize_display_attachments(attachments: Any) -> list[dict[str, Any]]:
    if not isinstance(attachments, list):
        return []

    sanitized: list[dict[str, Any]] = []
    for item in attachments:
        if isinstance(item, str):
            file_id = item.strip()
            if file_id:
                sanitized.append({"file_id": file_id, "id": file_id, "type": "file"})
            continue

        if not isinstance(item, dict):
            continue

        clean: dict[str, Any] = {}
        for key in _SAFE_ATTACHMENT_KEYS:
            if key not in item:
                continue
            value = item.get(key)
            if key == "size":
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                clean[key] = value
                continue
            if key == "parsed":
                if isinstance(value, bool):
                    clean[key] = value
                continue
            if key == "type":
                attachment_type = _clean_string(value)
                clean[key] = attachment_type if attachment_type in _ALLOWED_ATTACHMENT_TYPES else "file"
                continue
            if key in _STRING_ATTACHMENT_KEYS:
                text = _clean_string(value)
                if text:
                    clean[key] = text

        clean["type"] = clean.get("type") if clean.get("type") in _ALLOWED_ATTACHMENT_TYPES else "file"
        sanitized.append(clean)

    return sanitized


class UserDisplayStore:
    def __init__(self, path: Path):
        self.path = path
        self._payload: dict[str, Any] = {"version": 1, "messages": {}}
        self.load()

    def _key(self, opencode_session_id: str, opencode_message_id: str) -> str:
        return f"{opencode_session_id}:{opencode_message_id}"

    def _fresh_payload(self) -> dict[str, Any]:
        return {"version": 1, "messages": {}}

    def _quarantine_corrupt_file(self) -> Path | None:
        if not self.path.exists():
            return None
        stamp = utc_now_iso().replace(":", "").replace("+", "_").replace("/", "_")
        backup = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        try:
            self.path.replace(backup)
            return backup
        except Exception:
            return None

    def load(self) -> None:
        if not self.path.exists():
            self._payload = self._fresh_payload()
            return

        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            self._quarantine_corrupt_file()
            self._payload = self._fresh_payload()
            return

        if not isinstance(payload, dict) or not isinstance(payload.get("messages"), dict):
            self._payload = self._fresh_payload()
            return

        self._payload = {
            "version": 1,
            "messages": {str(k): v for k, v in payload.get("messages", {}).items() if isinstance(v, dict)},
        }

    reload = load

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        tmp_path.write_text(json.dumps(self._payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(self.path)

    def put_user_message(
        self,
        *,
        portal_session_id: str,
        opencode_session_id: str,
        opencode_message_id: str,
        display_content: str,
        display_attachments: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        portal_session_id = str(portal_session_id or "")
        opencode_session_id = str(opencode_session_id or "")
        opencode_message_id = str(opencode_message_id or "")
        now = utc_now_iso()
        key = self._key(opencode_session_id, opencode_message_id)
        existing = self._payload.setdefault("messages", {}).get(key)
        created_at = existing.get("created_at") if isinstance(existing, dict) and isinstance(existing.get("created_at"), str) else now
        record = {
            "portal_session_id": portal_session_id,
            "opencode_session_id": opencode_session_id,
            "opencode_message_id": opencode_message_id,
            "role": "user",
            "display_content": str(display_content or ""),
            "display_attachments": sanitize_display_attachments(display_attachments),
            "metadata": metadata if isinstance(metadata, dict) else {},
            "created_at": created_at,
            "updated_at": now,
        }
        self._payload["messages"][key] = record
        self._write()
        return record

    def get_user_message(
        self,
        opencode_session_id: str,
        opencode_message_id: str,
        portal_session_id: str | None = None,
    ) -> dict[str, Any] | None:
        opencode_session_id = str(opencode_session_id or "")
        opencode_message_id = str(opencode_message_id or "")
        direct = self._payload.get("messages", {}).get(self._key(opencode_session_id, opencode_message_id))
        if isinstance(direct, dict):
            return dict(direct)

        if not portal_session_id or not opencode_message_id:
            return None

        matches = [
            record
            for record in self._payload.get("messages", {}).values()
            if isinstance(record, dict)
            and record.get("opencode_message_id") == opencode_message_id
            and record.get("portal_session_id") == portal_session_id
        ]
        if len(matches) == 1:
            return dict(matches[0])
        return None

    def delete_session(self, portal_session_id: str | None = None, opencode_session_id: str | None = None) -> int:
        portal_session_id = str(portal_session_id or "")
        opencode_session_id = str(opencode_session_id or "")
        if not portal_session_id and not opencode_session_id:
            return 0

        messages = self._payload.setdefault("messages", {})
        keys_to_delete = [
            key
            for key, record in messages.items()
            if isinstance(record, dict)
            and (
                (portal_session_id and record.get("portal_session_id") == portal_session_id)
                or (opencode_session_id and record.get("opencode_session_id") == opencode_session_id)
            )
        ]
        for key in keys_to_delete:
            messages.pop(key, None)
        if keys_to_delete:
            self._write()
        return len(keys_to_delete)

    def sanitize_display_attachments(self, attachments: Any) -> list[dict[str, Any]]:
        return sanitize_display_attachments(attachments)
