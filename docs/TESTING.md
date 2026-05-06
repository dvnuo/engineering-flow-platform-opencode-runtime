# Testing

- `python -m pytest -q`
- `bash scripts/ci_unit.sh`
- `bash scripts/smoke.sh`
- `RUN_RUNTIME_CONTRACT_TESTS=1 bash scripts/smoke.sh`
- `RUNTIME_BASE_URL=http://localhost:8000 python -m pytest -q runtime_contract_tests`

By default runtime contract tests do not require Portal startup, K8s, or an LLM key.

Live checks are opt-in:
- `RUNTIME_CONTRACT_ENABLE_CHAT=1`
- `RUNTIME_CONTRACT_ENABLE_TASKS=1`

`scripts/smoke.sh` mounts deterministic skill/tool fixtures and validates restart persistence.
