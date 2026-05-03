from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path


class UsageTracker:
    def __init__(self, usage_file: Path):
        self.usage_file = usage_file

    def record_chat(self, *, session_id: str, request_id: str, model: str | None, provider: str | None, response_payload: dict | None, input_text: str, output_text: str) -> dict:
        usage = (response_payload or {}).get("usage") if isinstance(response_payload, dict) else {}
        input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        cost = float(usage.get("cost") or usage.get("total_cost") or 0.0)
        rec = {"timestamp": datetime.now(UTC).isoformat(), "type": "chat", "session_id": session_id, "request_id": request_id, "model": model or (response_payload or {}).get("model") or "unknown", "provider": provider or (response_payload or {}).get("provider") or "unknown", "requests": 1, "messages": 2, "input_tokens": input_tokens, "output_tokens": output_tokens, "cost": cost}
        self.usage_file.parent.mkdir(parents=True, exist_ok=True)
        with self.usage_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    def summarize(self, *, days: int = 30) -> dict:
        days = min(365, max(1, int(days)))
        cutoff = datetime.now(UTC) - timedelta(days=days)
        rows = []
        if self.usage_file.exists():
            for line in self.usage_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    ts = datetime.fromisoformat(row.get("timestamp", "").replace("Z", "+00:00"))
                    if ts >= cutoff:
                        rows.append(row)
                except Exception:
                    pass
        def agg(items, key):
            out = {}
            for r in items:
                k = r.get(key, "unknown") or "unknown"
                x = out.setdefault(k, {key: k, "total_requests": 0, "total_messages": 0, "total_input_tokens": 0, "total_output_tokens": 0, "total_cost": 0.0})
                x["total_requests"] += int(r.get("requests", 0)); x["total_messages"] += int(r.get("messages", 0)); x["total_input_tokens"] += int(r.get("input_tokens", 0)); x["total_output_tokens"] += int(r.get("output_tokens", 0)); x["total_cost"] += float(r.get("cost", 0.0))
            return list(out.values())
        g = {"total_requests": sum(int(r.get("requests",0)) for r in rows), "total_messages": sum(int(r.get("messages",0)) for r in rows), "total_input_tokens": sum(int(r.get("input_tokens",0)) for r in rows), "total_output_tokens": sum(int(r.get("output_tokens",0)) for r in rows), "total_cost": sum(float(r.get("cost",0.0)) for r in rows)}
        return {"period_days": days, "global": g, "by_model": agg(rows, "model"), "by_provider": agg(rows, "provider")}
