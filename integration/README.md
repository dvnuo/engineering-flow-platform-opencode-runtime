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
