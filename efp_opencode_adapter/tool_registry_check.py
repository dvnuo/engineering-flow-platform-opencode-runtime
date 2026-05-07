from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time

from .opencode_client import OpenCodeClient
from .settings import Settings


async def _run(timeout: int, request_timeout: int, expected_tools: list[str], opencode_url: str | None) -> int:
    settings = Settings.from_env(opencode_url=opencode_url)
    client = OpenCodeClient(settings)
    deadline = time.monotonic() + timeout
    attempt = 0
    last_error: Exception | None = None
    last_ids: list[str] | None = None

    while time.monotonic() < deadline:
        attempt += 1
        remaining = max(0.1, deadline - time.monotonic())
        per_request = min(request_timeout, remaining)
        try:
            ids = await client.list_tool_ids(timeout_seconds=int(math.ceil(per_request)))
            missing = [tool for tool in expected_tools if tool not in ids]
            if missing:
                last_ids = ids
                last_error = RuntimeError(f"missing expected tools {missing}")
            else:
                print(
                    json.dumps(
                        {
                            "status": "ok",
                            "tool_count": len(ids),
                            "expected_tools": expected_tools,
                            "missing_tools": [],
                            "attempts": attempt,
                        },
                        sort_keys=True,
                    )
                )
                return 0
        except Exception as exc:
            last_error = exc
        await asyncio.sleep(min(1.0, max(0.0, deadline - time.monotonic())))

    if isinstance(last_error, RuntimeError) and str(last_error).startswith("missing expected tools"):
        print(
            f"OpenCode ToolRegistry readiness check failed: {last_error}; attempts={attempt}; ids={last_ids}",
            file=sys.stderr,
        )
        return 1

    print(
        f"OpenCode ToolRegistry readiness check failed after {attempt} attempts over {timeout}s: {last_error}",
        file=sys.stderr,
    )
    return 1


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=int, default=int(os.getenv("EFP_OPENCODE_TOOL_REGISTRY_TIMEOUT_SECONDS", "60")))
    parser.add_argument("--request-timeout", type=int, default=int(os.getenv("EFP_OPENCODE_TOOL_REGISTRY_REQUEST_TIMEOUT_SECONDS", "15")))
    parser.add_argument("--expected-tool", action="append", default=[])
    parser.add_argument("--opencode-url", default=None)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args.timeout, args.request_timeout, args.expected_tool, args.opencode_url)))


if __name__ == "__main__":
    main()
