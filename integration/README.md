# OpenCode Runtime T13 Integration Smoke

This directory provides lightweight integration smoke entrypoints for the OpenCode runtime T13 acceptance.

- No Portal startup required.
- No Kubernetes required.
- No real LLM key required for default smoke.
- Optional live chat/task contract checks require:
  - `RUNTIME_CONTRACT_ENABLE_CHAT=1`
  - `RUNTIME_CONTRACT_ENABLE_TASKS=1`
  - working OpenCode provider/auth configuration.

CI docker-smoke runs `scripts/smoke.sh` with `RUN_RUNTIME_CONTRACT_TESTS=1`.
Default `runtime_contract_tests` do not require a live LLM key; chat/task checks remain opt-in via `RUNTIME_CONTRACT_ENABLE_CHAT=1` and `RUNTIME_CONTRACT_ENABLE_TASKS=1`.

Smoke contract runtime timeout can be tuned with `RUNTIME_CONTRACT_TIMEOUT_SECONDS` (default 120).

When `RUN_RUNTIME_CONTRACT_TESTS=1` is used, `scripts/smoke.sh` supports a skills-only expectation:
- `RUNTIME_CONTRACT_EXPECT_SKILL=smoke-skill`

Runtime contract checks do not validate external tools, tool mappings, tool indexes, or generated wrappers.
Tool surface is OpenCode-owned: OpenCode built-ins, OpenCode MCP (when enabled by OpenCode itself), runtime profile, and permission policy.
Source skill `tools` / `task_tools` metadata is informational only.

This is runtime-only smoke, not full Portal E2E. It validates runtime contract and state persistence. It does not validate real Portal K8s provisioning. Live chat/task checks are opt-in.
