from __future__ import annotations

import secrets
import threading
import time

_BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_lock = threading.Lock()
_last_ms = 0
_counter = 0


def new_opencode_message_id() -> str:
    """Generate an OpenCode-compatible message id."""
    global _last_ms, _counter
    now_ms = int(time.time() * 1000)
    with _lock:
        if now_ms != _last_ms:
            _last_ms = now_ms
            _counter = 0
        _counter = (_counter + 1) & 0xFFF
        time_part = f"{((now_ms * 0x1000) + _counter) & 0xFFFFFFFFFFFF:012x}"
    random_part = "".join(secrets.choice(_BASE62) for _ in range(14))
    return f"msg_{time_part}{random_part}"


def is_opencode_message_id(value: object) -> bool:
    return isinstance(value, str) and value.startswith("msg") and len(value.strip()) >= 4


def require_opencode_message_id(value: object, *, field: str = "messageID") -> str:
    if is_opencode_message_id(value):
        return str(value)
    raise ValueError(f"{field} must be an OpenCode message id starting with 'msg'")
