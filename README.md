# engineering-flow-platform-opencode-runtime

This repository contains the **T05-T13 OpenCode runtime adapter** for an EFP-compatible OpenCode runtime image.

## Runtime topology
- Portal-facing runtime endpoint: `0.0.0.0:8000`
- Internal native OpenCode server: `127.0.0.1:4096`
- OpenCode is installed from Docker build arg `OPENCODE_VERSION`; the adapter reports the observed OpenCode version and does not enforce an exact runtime version match.

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




## Assets contract (Portal vs runtime)
- Portal only provides **skills** inputs (for example repo/branch mounted to `EFP_SKILLS_DIR`, default `/app/skills`).
- `EFP_TOOLS_DIR` (default `/app/tools`) is treated as an **optional local/runtime-owned tools directory**.
- Missing `/app/tools`, empty `/app/tools`, or `/app/tools` without `manifest.yaml|manifest.yml` are all valid startup states; runtime still boots and serves chat/skills/capabilities APIs.
- If a local tools manifest is explicitly provided, runtime still enforces generator/index validation for that local asset.

For smoke/runtime contract tests, `RUNTIME_CONTRACT_EXPECT_*` variables are fixture injection knobs for explicit local tools fixtures; they do **not** mean Portal configures or clones a tools repo.

## CI note

GitHub Actions CI is intentionally disabled for now. Local validation remains available through `python -m pytest -q`, `bash scripts/ci_unit.sh`, and `bash scripts/smoke.sh`.

Do not recreate `.github/workflows/ci.yml` unless CI ownership and stable gates are reintroduced.

## Docs
- [docs/RUNTIME_CONTRACT.md](docs/RUNTIME_CONTRACT.md)
- [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md)
- [docs/TESTING.md](docs/TESTING.md)

## P3 contract gates
- observability trace fields
- runtime contract tests
- docker smoke asset mapping
- wrapper/tools-index snapshot tests

## Runtime permission/chat defaults
- `EFP_OPENCODE_PERMISSION_MODE=workspace_full_access` (default)
- `EFP_OPENCODE_ALLOW_BASH_ALL=true` (default)
- Default runtime permission map allows `edit`/`write` and forces `bash: {"*": "allow"}`.
- Set `profile_policy` to restore legacy ask/deny behavior for tighter environments.
- Chat final-state contract: `completed` + visible text => `ok=true`; `blocked`/`incomplete`/`error`/`empty_final` => `ok=false`; empty success responses are not allowed.
