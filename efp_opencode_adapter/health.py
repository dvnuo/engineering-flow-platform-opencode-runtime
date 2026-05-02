from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .opencode_client import OpenCodeClient
from .settings import Settings


async def _run(wait: bool, timeout: int, opencode_url: str | None) -> int:
    settings = Settings.from_env(opencode_url=opencode_url)
    client = OpenCodeClient(settings)
    if wait:
        try:
            await client.wait_until_ready(timeout_seconds=timeout)
            print("opencode ready")
            return 0
        except Exception as exc:
            print(str(exc), file=sys.stderr)
            return 1
    health = await client.health()
    print(json.dumps(health))
    return 0 if health.get("healthy") else 1


def main() -> None:
    defaults = Settings.from_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--timeout", type=int, default=defaults.ready_timeout_seconds)
    parser.add_argument("--opencode-url", default=None)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args.wait, args.timeout, args.opencode_url)))


if __name__ == "__main__":
    main()
