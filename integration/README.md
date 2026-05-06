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

When `RUN_RUNTIME_CONTRACT_TESTS=1` is used, `scripts/smoke.sh` sets smoke-specific asset expectations:
- `RUNTIME_CONTRACT_EXPECT_SKILL=smoke-skill`
- `RUNTIME_CONTRACT_EXPECT_LEGACY_TOOL=smoke_tool`
- `RUNTIME_CONTRACT_EXPECT_OPENCODE_TOOL=efp_smoke_tool`
- `RUNTIME_CONTRACT_EXPECT_TOOL_MAPPING=smoke_tool:efp_smoke_tool`

These checks validate the skill/tool asset bridge through `/api/skills` and `/api/capabilities`. Chat/task contract checks remain opt-in via `RUNTIME_CONTRACT_ENABLE_CHAT=1` and `RUNTIME_CONTRACT_ENABLE_TASKS=1`.


This is runtime-only smoke, not full Portal E2E. It validates asset bridge, runtime contract, and state persistence. It does not validate real Portal K8s provisioning. Live chat/task checks are opt-in.
