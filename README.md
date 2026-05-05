# engineering-flow-platform-opencode-runtime

This repository contains the **T05-T13 OpenCode runtime adapter** for an EFP-compatible OpenCode runtime image.

## Runtime topology
- Portal-facing runtime endpoint: `0.0.0.0:8000`
- Internal native OpenCode server: `127.0.0.1:4096`
- OpenCode version pinned to `1.14.29`

## Status
- T12:
  - thinking events
  - usage tracker
  - recovery manager
  - portal metadata client
- T13:
  - runtime contract tests
  - docker smoke
  - CI
  - `/api/skills`
  - `/api/queue/status`
  - git info compatibility
  - system prompt compatibility

## Local development
```bash
python -m pytest -q
bash scripts/ci_unit.sh
bash scripts/smoke.sh
RUN_RUNTIME_CONTRACT_TESTS=1 bash scripts/smoke.sh  # acceptance smoke (runs runtime_contract_tests)
RUNTIME_BASE_URL=http://localhost:8000 python -m pytest -q runtime_contract_tests
```

`RUN_RUNTIME_CONTRACT_TESTS=1 bash scripts/smoke.sh` passes asset mapping expectations into runtime_contract_tests. Preferred variables:
- `RUNTIME_CONTRACT_EXPECT_SKILL`
- `RUNTIME_CONTRACT_EXPECT_LEGACY_TOOL`
- `RUNTIME_CONTRACT_EXPECT_OPENCODE_TOOL`
- `RUNTIME_CONTRACT_EXPECT_TOOL_MAPPING=legacy:opencode`

Legacy aliases `RUNTIME_CONTRACT_EXPECT_TOOL` and `RUNTIME_CONTRACT_EXPECT_EFP_TOOL` remain supported for backward compatibility. Default contract runs do not enable live chat/task LLM checks.
