from pathlib import Path


FORBIDDEN = [
    "ChatRunStore",
    "CHAT_RUN_STORE_KEY",
    "chat_run_already_active",
    "/api/chat/runs",
    "active-run",
    "hard-reset",
    "stream_detached",
    "stream_attached",
    "timeout_recovery",
    "transport_recovery",
    "continuation.completed",
    "auto_continue",
    "AUTO_CONTINUE",
    "chat_total_wall",
    "no_progress",
]


def test_no_long_run_recovery_markers_in_production_code():
    root = Path("efp_opencode_adapter")
    offenders = {}
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        hits = [marker for marker in FORBIDDEN if marker in text]
        if hits:
            offenders[str(path)] = hits

    assert offenders == {}
