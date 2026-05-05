#!/usr/bin/env bash
set -euo pipefail

NAME="efp-opencode-runtime-smoke"
HEALTH_FILE="$(mktemp)"
ASSET_ROOT="$(mktemp -d)"
WORKSPACE_DIR="${ASSET_ROOT}/workspace"
ADAPTER_STATE_DIR="${ASSET_ROOT}/adapter-state"
OPENCODE_STATE_DIR="${ASSET_ROOT}/opencode-state"
SKILLS_DIR="${ASSET_ROOT}/skills"
TOOLS_DIR="${ASSET_ROOT}/tools"
RUN_RUNTIME_CONTRACT_TESTS="${RUN_RUNTIME_CONTRACT_TESTS:-0}"
RUNTIME_CONTRACT_BASE_URL="${RUNTIME_CONTRACT_BASE_URL:-http://localhost:8000}"

dump_logs_on_failure() {
  local status="$1"
  if [[ "${status}" -ne 0 ]]; then
    echo "smoke failed with status ${status}; docker logs follow:" >&2
    docker logs "${NAME}" >&2 || true
  fi
}

cleanup() {
  rm -f "${HEALTH_FILE}" >/dev/null 2>&1 || true
  rm -rf "${ASSET_ROOT}" >/dev/null 2>&1 || true
  docker rm -f "${NAME}" >/dev/null 2>&1 || true
}

on_exit() {
  local status="$?"
  dump_logs_on_failure "${status}"
  cleanup
  exit "${status}"
}

trap on_exit EXIT

mkdir -p "${WORKSPACE_DIR}" "${ADAPTER_STATE_DIR}" "${OPENCODE_STATE_DIR}" "${SKILLS_DIR}/smoke_skill" "${TOOLS_DIR}/adapters/opencode"
cat > "${SKILLS_DIR}/smoke_skill/skill.md" <<'SKILL'
---
name: smoke_skill
description: Smoke skill for OpenCode runtime asset bridge
risk_level: low
tools:
  - smoke_tool
task_tools: []
---

This is a deterministic smoke skill. It should be converted into OpenCode SKILL.md.
SKILL
cat > "${TOOLS_DIR}/manifest.yaml" <<'MANIFEST'
tools:
  - capability_id: smoke.tool
    name: smoke_tool
    opencode_name: efp_smoke_tool
    description: Smoke read-only tool
    enabled: true
    runtime_compat: [opencode]
    policy_tags: [read_only, smoke]
    input_schema:
      type: object
      properties:
        query:
          type: string
      required: [query]
MANIFEST
cat > "${TOOLS_DIR}/adapters/opencode/generate_tools.py" <<'PY'
#!/usr/bin/env python3
import argparse, json
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument('--tools-dir'); p.add_argument('--opencode-tools-dir'); p.add_argument('--state-dir'); a=p.parse_args()
out=Path(a.opencode_tools_dir); out.mkdir(parents=True, exist_ok=True)
(out/'efp_smoke_tool.ts').write_text('export default async function efp_smoke_tool() { return { ok: true }; }\n', encoding='utf-8')
state=Path(a.state_dir); state.mkdir(parents=True, exist_ok=True)
(state/'tools-index.json').write_text(json.dumps({'tools':[{'capability_id':'smoke.tool','tool_id':'smoke.tool','name':'efp_smoke_tool','opencode_name':'efp_smoke_tool','legacy_name':'smoke_tool','description':'Smoke read-only tool','enabled':True,'policy_tags':['read_only','smoke'],'runtime_compat':['opencode'],'risk_level':'low','requires_identity_binding':False,'type':'adapter_action','source_ref':'scripts/smoke.sh'}]}), encoding='utf-8')
PY
chmod +x "${TOOLS_DIR}/adapters/opencode/generate_tools.py"

run_runtime_contract_tests() {
  if [[ "${RUN_RUNTIME_CONTRACT_TESTS}" != "1" ]]; then
    return 0
  fi

  echo "running runtime_contract_tests against ${RUNTIME_CONTRACT_BASE_URL}"
  RUNTIME_BASE_URL="${RUNTIME_CONTRACT_BASE_URL}" python -m pytest -q runtime_contract_tests
}

docker build -t efp-opencode-runtime:test .
docker run -d --name "${NAME}" -p 8000:8000 -e OPENCODE_SERVER_PASSWORD=test-password -e OPENCODE_DATA_DIR=/home/opencode/.local/share/opencode -e EFP_ADAPTER_STATE_DIR=/home/opencode/.local/share/efp-compat -v "${WORKSPACE_DIR}:/workspace" -v "${ADAPTER_STATE_DIR}:/home/opencode/.local/share/efp-compat" -v "${OPENCODE_STATE_DIR}:/home/opencode/.local/share/opencode" -v "${SKILLS_DIR}:/app/skills:ro" -v "${TOOLS_DIR}:/app/tools:ro" efp-opencode-runtime:test >/dev/null

wait_health() {
  : > "${HEALTH_FILE}"
  for _ in $(seq 1 60); do
    if curl -fsS http://localhost:8000/health >"${HEALTH_FILE}"; then
      return 0
    fi
    sleep 1
  done
  echo "health did not become ready" >&2
  docker logs "${NAME}" >&2 || true
  return 1
}

assert_health_state() {
  jq -e '.state.healthy == true' "${HEALTH_FILE}" >/dev/null
  jq -e '.state.paths.adapter_state_dir.writable == true' "${HEALTH_FILE}" >/dev/null
  jq -e '.state.paths.opencode_data_dir.writable == true' "${HEALTH_FILE}" >/dev/null
  jq -e '.event_bridge.enabled == true' "${HEALTH_FILE}" >/dev/null
}

wait_health
assert_health_state

docker exec "${NAME}" test -f /workspace/.opencode/skills/smoke-skill/SKILL.md
docker exec "${NAME}" test -f /workspace/.opencode/tools/efp_smoke_tool.ts
docker exec "${NAME}" test -f /home/opencode/.local/share/efp-compat/skills-index.json
docker exec "${NAME}" test -f /home/opencode/.local/share/efp-compat/tools-index.json
docker exec "${NAME}" sh -lc "grep -q 'smoke_tool -> efp_smoke_tool' /workspace/.opencode/skills/smoke-skill/SKILL.md"
docker exec "${NAME}" sh -lc "jq -e '.skills[] | select(.opencode_name == \"smoke-skill\") | .opencode_tools | index(\"efp_smoke_tool\")' /home/opencode/.local/share/efp-compat/skills-index.json >/dev/null"
docker exec "${NAME}" sh -lc "jq -e '.skills[] | select(.opencode_name == \"smoke-skill\") | .tool_mappings[] | select(.efp_name == \"smoke_tool\" and .opencode_name == \"efp_smoke_tool\" and .available == true)' /home/opencode/.local/share/efp-compat/skills-index.json >/dev/null"
curl -fsS http://localhost:8000/api/skills | jq -e '.count >= 1' >/dev/null
curl -fsS http://localhost:8000/api/skills | jq -e '.skills[] | select(.name == "smoke-skill")' >/dev/null
curl -fsS http://localhost:8000/api/skills | jq -e '.skills[] | select(.name == "smoke-skill") | .opencode_tools | index("efp_smoke_tool")' >/dev/null
curl -fsS http://localhost:8000/api/capabilities | jq -e '.capabilities[] | select(.name == "smoke-skill" and .type == "skill")' >/dev/null
curl -fsS http://localhost:8000/api/capabilities | jq -e '.capabilities[] | select(.name == "smoke-skill" and .type == "skill") | (.opencode_tools // .metadata.opencode_tools) | index("efp_smoke_tool")' >/dev/null
curl -fsS http://localhost:8000/api/capabilities | jq -e '.capabilities[] | select(.name == "efp_smoke_tool")' >/dev/null
run_runtime_contract_tests

docker exec "${NAME}" sh -lc 'echo adapter-persist > /home/opencode/.local/share/efp-compat/persistence-sentinel.txt'
docker exec "${NAME}" sh -lc 'echo opencode-persist > /home/opencode/.local/share/opencode/persistence-sentinel.txt'
docker restart "${NAME}" >/dev/null
wait_health
assert_health_state
docker exec "${NAME}" test -f /home/opencode/.local/share/efp-compat/persistence-sentinel.txt
docker exec "${NAME}" test -f /home/opencode/.local/share/opencode/persistence-sentinel.txt
docker exec "${NAME}" test -f /workspace/.opencode/skills/smoke-skill/SKILL.md
docker exec "${NAME}" test -f /workspace/.opencode/tools/efp_smoke_tool.ts
docker exec "${NAME}" sh -lc "grep -q 'smoke_tool -> efp_smoke_tool' /workspace/.opencode/skills/smoke-skill/SKILL.md"
docker exec "${NAME}" sh -lc "jq -e '.skills[] | select(.opencode_name == \"smoke-skill\") | .opencode_tools | index(\"efp_smoke_tool\")' /home/opencode/.local/share/efp-compat/skills-index.json >/dev/null"
run_runtime_contract_tests

echo "smoke passed"
