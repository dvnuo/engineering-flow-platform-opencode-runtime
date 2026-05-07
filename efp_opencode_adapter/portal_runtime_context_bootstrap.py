from __future__ import annotations
import argparse, asyncio, hashlib, json, os
from pathlib import Path
import aiohttp
from .opencode_config import build_opencode_config, normalize_opencode_provider_id, write_opencode_config
from .settings import Settings


def _sanitize(text: str) -> str:
    for key in (os.getenv("PORTAL_INTERNAL_TOKEN", ""), os.getenv("OPENCODE_SERVER_PASSWORD", "")):
        if key:
            text = text.replace(key, "***REDACTED***")
    return text


async def _run(workspace_dir: Path) -> int:
    settings = Settings.from_env()
    base = os.getenv("PORTAL_INTERNAL_BASE_URL")
    agent_id = os.getenv("PORTAL_AGENT_ID")
    if not base or not agent_id:
        print(json.dumps({"status": "skipped", "reason": "portal context env missing"}))
        return 0
    token = os.getenv("PORTAL_INTERNAL_TOKEN", "")
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-Portal-Internal-Token"] = token
    url = f"{base.rstrip('/')}/api/internal/agents/{agent_id}/runtime-context"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as r:
                r.raise_for_status()
                data = await r.json()
    except Exception as exc:
        if os.getenv("EFP_REQUIRE_PORTAL_RUNTIME_CONTEXT", "false").lower() == "true":
            print(json.dumps({"status": "error", "reason": "portal context fetch failed", "error": _sanitize(repr(exc))}))
            return 1
        print(json.dumps({"status": "skipped", "reason": "portal context fetch failed", "error": _sanitize(repr(exc))}))
        return 0
    profile = data.get("runtime_profile_context") if isinstance(data, dict) else {}
    runtime_config = (profile or {}).get("config") if isinstance(profile, dict) else {}
    runtime_profile_id = (profile or {}).get("runtime_profile_id") if isinstance(profile, dict) else None
    revision = (profile or {}).get("revision") if isinstance(profile, dict) else None
    generated, config_hash, _ = build_opencode_config(settings, runtime_config if isinstance(runtime_config, dict) else {})
    settings.opencode_config_path = workspace_dir / ".opencode" / "opencode.json"
    write_opencode_config(settings, generated)

    llm = runtime_config.get("llm") if isinstance(runtime_config, dict) and isinstance(runtime_config.get("llm"), dict) else {}
    provider = normalize_opencode_provider_id(llm.get("provider"))
    model = generated.get("agent", {}).get("efp-main", {}).get("model")
    auth_written = False
    if provider and isinstance(llm.get("api_key"), str) and llm.get("api_key"):
        auth_path = settings.opencode_data_dir / "auth.json"
        auth_path.parent.mkdir(parents=True, exist_ok=True)
        existing = {}
        if auth_path.exists():
            try:
                existing = json.loads(auth_path.read_text(encoding="utf-8"))
            except Exception:
                existing = {}
        if not isinstance(existing, dict):
            existing = {}
        existing[provider] = {"type": "api", "key": llm.get("api_key")}
        auth_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
        auth_path.chmod(0o600)
        auth_written = True
    print(json.dumps({"status": "ok", "runtime_profile_id": runtime_profile_id, "revision": revision, "provider": provider or None, "model": model, "auth_written": auth_written, "config_written": True, "config_hash": config_hash}))
    return 0


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--workspace-dir", default=os.getenv("EFP_WORKSPACE_DIR", "/workspace"))
    args = p.parse_args()
    raise SystemExit(asyncio.run(_run(Path(args.workspace_dir))))

if __name__ == "__main__":
    main()
