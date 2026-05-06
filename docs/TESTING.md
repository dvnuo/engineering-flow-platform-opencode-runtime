# Testing

## Local unit tests
- `python -m pytest -q`

## CI unit script
- `bash scripts/ci_unit.sh`

## Runtime contract tests
- `RUNTIME_BASE_URL=http://localhost:8000 python -m pytest -q runtime_contract_tests`
- Default behavior with no `RUNTIME_BASE_URL`: skip instead of fail.

## Docker smoke
- `bash scripts/smoke.sh`
- `RUN_RUNTIME_CONTRACT_TESTS=1 bash scripts/smoke.sh`

## Runtime-only vs Portal E2E
These tests validate runtime adapter contract only, not full Portal/K8s provisioning E2E.

## Packaging install check
- `python -m pip install -e .`
- `python -m pip install -e ".[test]"`

## Live LLM opt-in
- `RUNTIME_CONTRACT_ENABLE_CHAT=1`
- `RUNTIME_CONTRACT_ENABLE_TASKS=1`

## Troubleshooting
- pip package discovery failure: ensure setuptools include/exclude only packages `efp_opencode_adapter*`.
- runtime_contract_tests all skipped: set `RUNTIME_BASE_URL`.
- docker smoke cannot reach `/health`: check adapter port `:8000` bind and container status.
- state persistence fails after restart: verify mounted state dirs are writable/persistent.
