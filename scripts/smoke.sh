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
RUNTIME_CONTRACT_TIMEOUT_SECONDS="${RUNTIME_CONTRACT_TIMEOUT_SECONDS:-120}"
RUNTIME_CONTRACT_EXPECT_SKILL="${RUNTIME_CONTRACT_EXPECT_SKILL:-}"
RUNTIME_CONTRACT_EXPECT_LEGACY_TOOL="${RUNTIME_CONTRACT_EXPECT_LEGACY_TOOL:-${RUNTIME_CONTRACT_EXPECT_TOOL:-}}"
RUNTIME_CONTRACT_EXPECT_OPENCODE_TOOL="${RUNTIME_CONTRACT_EXPECT_OPENCODE_TOOL:-${RUNTIME_CONTRACT_EXPECT_EFP_TOOL:-}}"
RUNTIME_CONTRACT_EXPECT_TOOL_MAPPING="${RUNTIME_CONTRACT_EXPECT_TOOL_MAPPING:-}"
RUNTIME_CONTRACT_EXPECT_TOOL="${RUNTIME_CONTRACT_EXPECT_TOOL:-${RUNTIME_CONTRACT_EXPECT_LEGACY_TOOL}}"
RUNTIME_CONTRACT_EXPECT_EFP_TOOL="${RUNTIME_CONTRACT_EXPECT_EFP_TOOL:-${RUNTIME_CONTRACT_EXPECT_OPENCODE_TOOL}}"
OPENCODE_VERSION="${OPENCODE_VERSION:-1.14.39}"

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
(out/'efp_smoke_tool.ts').write_text('import { tool } from "@opencode-ai/plugin"\n\nexport default tool({\n  description: "Smoke read-only tool",\n  args: {\n    query: tool.schema.string().describe("query")\n  },\n  async execute(args, context) {\n    return {\n      output: JSON.stringify({\n        ok: true,\n        query: args.query,\n        session_id: context.sessionID,\n        runtime_type: "opencode"\n      })\n    }\n  }\n})\n', encoding='utf-8')
state=Path(a.state_dir); state.mkdir(parents=True, exist_ok=True)
(state/'tools-index.json').write_text(json.dumps({'tools':[{'capability_id':'smoke.tool','tool_id':'smoke.tool','name':'efp_smoke_tool','opencode_name':'efp_smoke_tool','legacy_name':'smoke_tool','description':'Smoke read-only tool','enabled':True,'policy_tags':['read_only','smoke'],'runtime_compat':['opencode'],'risk_level':'low','requires_identity_binding':False,'type':'adapter_action','source_ref':'scripts/smoke.sh'}]}), encoding='utf-8')
PY
chmod +x "${TOOLS_DIR}/adapters/opencode/generate_tools.py"

mkdir -p "${WORKSPACE_DIR}/.opencode"
cat > "${WORKSPACE_DIR}/.opencode/package-lock.json" <<'LOCK'
{
  "name": "stale-opencode-workspace",
  "lockfileVersion": 3,
  "requires": true,
  "packages": {
    "": {
      "dependencies": {}
    }
  }
}
LOCK

mkdir -p "${WORKSPACE_DIR}/global-node-modules/@opencode-ai/plugin"
cat > "${WORKSPACE_DIR}/global-node-modules/@opencode-ai/plugin/package.json" <<'JSON'
{"name":"@opencode-ai/plugin","version":"old"}
JSON

ln -sfn "../global-node-modules" "${WORKSPACE_DIR}/.opencode/node_modules"

assert_node_tool_dependency_resolution() {
  docker exec "${NAME}" sh -lc 'cd /workspace/.opencode/tools && node --input-type=module -' <<'NODE'
import fs from "node:fs"
import { fileURLToPath } from "node:url"

const localPrefix = fs.realpathSync("/workspace/.opencode/node_modules") + "/"

const plugin = fileURLToPath(import.meta.resolve("@opencode-ai/plugin"))
const zod = fileURLToPath(import.meta.resolve("zod"))
const effect = fileURLToPath(import.meta.resolve("effect"))

const pluginModule = await import("@opencode-ai/plugin")
if (typeof pluginModule.tool !== "function") {
  throw new Error("@opencode-ai/plugin did not export a tool function")
}
if (!pluginModule.tool.schema) {
  throw new Error("@opencode-ai/plugin tool helper did not expose schema")
}

function assertLocal(label, value) {
  const real = fs.realpathSync(value)
  if (!real.startsWith(localPrefix)) {
    throw new Error(`${label} resolved outside workspace .opencode node_modules: ${value} -> ${real}`)
  }
  return real
}

const realPlugin = assertLocal("plugin", plugin)
const realZod = assertLocal("zod", zod)
const realEffect = assertLocal("effect", effect)

console.log(JSON.stringify({
  plugin,
  zod,
  effect,
  realPlugin,
  realZod,
  realEffect
}))
NODE
}

assert_opencode_tool_registry() {
  docker exec "${NAME}" python -m efp_opencode_adapter.tool_registry_check \
    --timeout 600 \
    --request-timeout 600 \
    --expected-tool efp_smoke_tool
}

assert_workspace_package_lock_declares_plugin() {
  docker exec "${NAME}" sh -lc '
    jq -e ".packages[\"\"].dependencies[\"@opencode-ai/plugin\"]" \
      /workspace/.opencode/package-lock.json >/dev/null
  '
}

assert_workspace_node_modules_is_local_directory() {
  docker exec "${NAME}" sh -lc '
    test -d /workspace/.opencode/node_modules
    test ! -L /workspace/.opencode/node_modules
  '
}

assert_opencode_binary_version() {
  docker exec "${NAME}" sh -lc '
    actual="$(opencode --version | grep -Eo "[0-9]+\.[0-9]+\.[0-9]+" | head -1)"
    test "${actual}" = "'"${OPENCODE_VERSION}"'"
    node -e "
const fs = require(\"fs\")
const pkg = JSON.parse(fs.readFileSync(\"/app/runtime/package.json\", \"utf8\"))
if (pkg.dependencies[\"opencode-ai\"] !== \"'"${OPENCODE_VERSION}"'\") { throw new Error(`package opencode-ai mismatch: ${pkg.dependencies[\"opencode-ai\"]}`) }
if (pkg.dependencies[\"@opencode-ai/plugin\"] !== \"'"${OPENCODE_VERSION}"'\") { throw new Error(`package @opencode-ai/plugin mismatch: ${pkg.dependencies[\"@opencode-ai/plugin\"]}`) }
"
  '
}

run_runtime_contract_tests() {
  if [[ "${RUN_RUNTIME_CONTRACT_TESTS}" != "1" ]]; then
    return 0
  fi

  echo "running runtime_contract_tests against ${RUNTIME_CONTRACT_BASE_URL}"
  env_args=("RUNTIME_BASE_URL=${RUNTIME_CONTRACT_BASE_URL}")
  [[ -n "${RUNTIME_CONTRACT_EXPECT_SKILL}" ]] && env_args+=("RUNTIME_CONTRACT_EXPECT_SKILL=${RUNTIME_CONTRACT_EXPECT_SKILL}")
  [[ -n "${RUNTIME_CONTRACT_EXPECT_LEGACY_TOOL}" ]] && env_args+=("RUNTIME_CONTRACT_EXPECT_LEGACY_TOOL=${RUNTIME_CONTRACT_EXPECT_LEGACY_TOOL}")
  [[ -n "${RUNTIME_CONTRACT_EXPECT_OPENCODE_TOOL}" ]] && env_args+=("RUNTIME_CONTRACT_EXPECT_OPENCODE_TOOL=${RUNTIME_CONTRACT_EXPECT_OPENCODE_TOOL}")
  [[ -n "${RUNTIME_CONTRACT_EXPECT_TOOL_MAPPING}" ]] && env_args+=("RUNTIME_CONTRACT_EXPECT_TOOL_MAPPING=${RUNTIME_CONTRACT_EXPECT_TOOL_MAPPING}")
  [[ -n "${RUNTIME_CONTRACT_EXPECT_TOOL}" ]] && env_args+=("RUNTIME_CONTRACT_EXPECT_TOOL=${RUNTIME_CONTRACT_EXPECT_TOOL}")
  [[ -n "${RUNTIME_CONTRACT_EXPECT_EFP_TOOL}" ]] && env_args+=("RUNTIME_CONTRACT_EXPECT_EFP_TOOL=${RUNTIME_CONTRACT_EXPECT_EFP_TOOL}")

  timeout "${RUNTIME_CONTRACT_TIMEOUT_SECONDS}" env "${env_args[@]}" \
    python -m pytest -q runtime_contract_tests
}

OPENCODE_VERSION="${OPENCODE_VERSION:-1.14.39}"

BUILD_FLAGS=()
if [[ "${EFP_SMOKE_NO_CACHE:-0}" == "1" ]]; then
  BUILD_FLAGS+=(--no-cache --pull)
fi

docker build "${BUILD_FLAGS[@]}" \
  --build-arg "OPENCODE_VERSION=${OPENCODE_VERSION}" \
  -t efp-opencode-runtime:test .
docker run -d --name "${NAME}" -p 8000:8000 -e OPENCODE_DATA_DIR=/root/.local/share/opencode -e EFP_ADAPTER_STATE_DIR=/root/.local/share/efp-compat -v "${WORKSPACE_DIR}:/workspace" -v "${ADAPTER_STATE_DIR}:/root/.local/share/efp-compat" -v "${OPENCODE_STATE_DIR}:/root/.local/share/opencode" -v "${SKILLS_DIR}:/app/skills:ro" -v "${TOOLS_DIR}:/app/tools:ro" efp-opencode-runtime:test >/dev/null

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
assert_opencode_binary_version
assert_node_tool_dependency_resolution
assert_opencode_tool_registry
assert_workspace_package_lock_declares_plugin
assert_workspace_node_modules_is_local_directory

docker exec "${NAME}" sh -lc 'test "$(id -u)" = "0"'
docker exec "${NAME}" sh -lc 'test "${HOME:-}" = "/root"'

docker exec "${NAME}" test -f /workspace/.opencode/skills/smoke-skill/SKILL.md
docker exec "${NAME}" test -f /workspace/.opencode/tools/efp_smoke_tool.ts
docker exec "${NAME}" test -f /workspace/.opencode/node_modules/@opencode-ai/plugin/package.json
docker exec "${NAME}" test -f /root/.local/share/efp-compat/skills-index.json
docker exec "${NAME}" test -f /root/.local/share/efp-compat/tools-index.json
docker exec "${NAME}" sh -lc "grep -q 'smoke_tool -> efp_smoke_tool' /workspace/.opencode/skills/smoke-skill/SKILL.md"
docker exec "${NAME}" sh -lc "jq -e '.skills[] | select(.opencode_name == \"smoke-skill\") | .opencode_tools | index(\"efp_smoke_tool\")' /root/.local/share/efp-compat/skills-index.json >/dev/null"
docker exec "${NAME}" sh -lc "jq -e '.skills[] | select(.opencode_name == \"smoke-skill\") | .tool_mappings[] | select(.efp_name == \"smoke_tool\" and .opencode_name == \"efp_smoke_tool\" and .available == true)' /root/.local/share/efp-compat/skills-index.json >/dev/null"
curl -fsS http://localhost:8000/api/skills | jq -e '.count >= 1' >/dev/null
curl -fsS http://localhost:8000/api/skills | jq -e '.skills[] | select(.name == "smoke-skill")' >/dev/null
curl -fsS http://localhost:8000/api/skills | jq -e '.skills[] | select(.name == "smoke-skill") | .opencode_tools | index("efp_smoke_tool")' >/dev/null
curl -fsS http://localhost:8000/api/capabilities | jq -e '.capabilities[] | select(.name == "smoke-skill" and .type == "skill")' >/dev/null
curl -fsS http://localhost:8000/api/capabilities | jq -e '.capabilities[] | select(.name == "smoke-skill" and .type == "skill") | (.opencode_tools // .metadata.opencode_tools) | index("efp_smoke_tool")' >/dev/null
curl -fsS http://localhost:8000/api/capabilities | jq -e '.capabilities[] | select(.name == "efp_smoke_tool")' >/dev/null
run_runtime_contract_tests

docker exec "${NAME}" sh -lc 'echo adapter-persist > /root/.local/share/efp-compat/persistence-sentinel.txt'
docker exec "${NAME}" sh -lc 'echo opencode-persist > /root/.local/share/opencode/persistence-sentinel.txt'
docker restart "${NAME}" >/dev/null
wait_health
assert_health_state
assert_opencode_binary_version
assert_node_tool_dependency_resolution
assert_opencode_tool_registry
assert_workspace_package_lock_declares_plugin
assert_workspace_node_modules_is_local_directory
docker exec "${NAME}" test -f /root/.local/share/efp-compat/persistence-sentinel.txt
docker exec "${NAME}" test -f /root/.local/share/opencode/persistence-sentinel.txt
docker exec "${NAME}" test -f /workspace/.opencode/skills/smoke-skill/SKILL.md
docker exec "${NAME}" test -f /workspace/.opencode/tools/efp_smoke_tool.ts
docker exec "${NAME}" test -f /workspace/.opencode/node_modules/@opencode-ai/plugin/package.json
docker exec "${NAME}" sh -lc "grep -q 'smoke_tool -> efp_smoke_tool' /workspace/.opencode/skills/smoke-skill/SKILL.md"
docker exec "${NAME}" sh -lc "jq -e '.skills[] | select(.opencode_name == \"smoke-skill\") | .opencode_tools | index(\"efp_smoke_tool\")' /root/.local/share/efp-compat/skills-index.json >/dev/null"
run_runtime_contract_tests

echo "smoke passed"
