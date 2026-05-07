from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from .opencode_client import OpenCodeClient
from .settings import Settings


async def _run(timeout: int, expected_tools: list[str], opencode_url: str | None) -> int:
    settings = Settings.from_env(opencode_url=opencode_url)
    client = OpenCodeClient(settings)
    try:
        ids = await client.list_tool_ids(timeout_seconds=timeout)
    except Exception as exc:
        print(f"OpenCode ToolRegistry readiness check failed: {exc}", file=sys.stderr)
        return 1

    missing = [tool for tool in expected_tools if tool not in ids]
    if missing:
        print(
            f"OpenCode ToolRegistry readiness check failed: missing expected tools {missing}",
            file=sys.stderr,
        )
        return 1

    print(
        json.dumps(
            {
                "status": "ok",
                "tool_count": len(ids),
                "expected_tools": expected_tools,
                "missing_tools": [],
            },
            sort_keys=True,
        )
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=int, default=int(os.getenv("EFP_OPENCODE_TOOL_REGISTRY_TIMEOUT_SECONDS", "30")))
    parser.add_argument("--expected-tool", action="append", default=[])
    parser.add_argument("--opencode-url", default=None)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args.timeout, args.expected_tool, args.opencode_url)))


if __name__ == "__main__":
    main()
