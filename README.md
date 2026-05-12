# engineering-flow-platform-opencode-runtime

OpenCode runtime adapter for EFP-compatible runtime image.

## Runtime topology
- Portal-facing adapter: `0.0.0.0:8000`
- Internal OpenCode server: `127.0.0.1:4096`

## Contract
- **External tools subsystem is removed / not supported**.
- Portal provides skills input only (`EFP_SKILLS_DIR`, default `/app/skills`).
- Tool capability comes from OpenCode built-in tools + runtime permission/profile policy.
- Runtime does not read/sync/generate/index external tools repos or manifests.

## Local development
```bash
python -m pytest -q
bash scripts/ci_unit.sh
bash scripts/smoke.sh
```
