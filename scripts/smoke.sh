#!/usr/bin/env bash
set -euo pipefail
NAME="efp-opencode-runtime-smoke"
HEALTH_FILE="$(mktemp)"
ASSET_ROOT="$(mktemp -d)"
WORKSPACE_DIR="${ASSET_ROOT}/workspace"
ADAPTER_STATE_DIR="${ASSET_ROOT}/adapter-state"
OPENCODE_STATE_DIR="${ASSET_ROOT}/opencode-state"
SKILLS_DIR="${ASSET_ROOT}/skills"
RUN_RUNTIME_CONTRACT_TESTS="${RUN_RUNTIME_CONTRACT_TESTS:-0}"
RUNTIME_CONTRACT_BASE_URL="${RUNTIME_CONTRACT_BASE_URL:-http://localhost:8000}"
RUNTIME_CONTRACT_TIMEOUT_SECONDS="${RUNTIME_CONTRACT_TIMEOUT_SECONDS:-120}"
mkdir -p "${WORKSPACE_DIR}" "${ADAPTER_STATE_DIR}" "${OPENCODE_STATE_DIR}" "${SKILLS_DIR}/smoke_skill"
cat > "${SKILLS_DIR}/smoke_skill/skill.md" <<'SKILL'
---
name: smoke_skill
description: Smoke skill for OpenCode runtime asset bridge
risk_level: low
tools: []
task_tools: []
---
Smoke skill.
SKILL
cleanup(){ rm -f "${HEALTH_FILE}"||true; rm -rf "${ASSET_ROOT}"||true; docker rm -f "${NAME}" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker build -t efp-opencode-runtime:test .
docker run -d --name "${NAME}" -p 8000:8000 -e OPENCODE_DATA_DIR=/root/.local/share/opencode -e EFP_ADAPTER_STATE_DIR=/root/.local/share/efp-compat -v "${WORKSPACE_DIR}:/workspace" -v "${ADAPTER_STATE_DIR}:/root/.local/share/efp-compat" -v "${OPENCODE_STATE_DIR}:/root/.local/share/opencode" -v "${SKILLS_DIR}:/app/skills:ro" efp-opencode-runtime:test >/dev/null
for _ in $(seq 1 60); do curl -fsS http://localhost:8000/health >"${HEALTH_FILE}" && break || true; sleep 1; done
jq -e '.state.healthy == true' "${HEALTH_FILE}" >/dev/null
curl -fsS http://localhost:8000/api/skills | jq -e '.count >= 1' >/dev/null
curl -fsS http://localhost:8000/api/capabilities | jq -e '.engine == "opencode"' >/dev/null
if [[ "${RUN_RUNTIME_CONTRACT_TESTS}" == "1" ]]; then
  timeout "${RUNTIME_CONTRACT_TIMEOUT_SECONDS}" env "RUNTIME_BASE_URL=${RUNTIME_CONTRACT_BASE_URL}" python -m pytest -q runtime_contract_tests
fi
echo "smoke passed"
