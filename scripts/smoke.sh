#!/usr/bin/env bash
set -euo pipefail
NAME="efp-opencode-runtime-smoke"
HEALTH_FILE="$(mktemp)"
ASSET_ROOT="$(mktemp -d)"
WORKSPACE_DIR="${ASSET_ROOT}/workspace"
ADAPTER_STATE_DIR="${ASSET_ROOT}/adapter-state"
OPENCODE_STATE_DIR="${ASSET_ROOT}/opencode-state"
SKILLS_DIR="${ASSET_ROOT}/skills"
MAVEN_SETTINGS_DIR="${MAVEN_SETTINGS_DIR:-runtime-maven}"
MAVEN_SETTINGS_PATH="${MAVEN_SETTINGS_DIR}/settings.xml"
SMOKE_CREATED_MAVEN_SETTINGS=0
VOLUME_LABEL_OPT=""
RUN_RUNTIME_CONTRACT_TESTS="${RUN_RUNTIME_CONTRACT_TESTS:-0}"
RUNTIME_CONTRACT_BASE_URL="${RUNTIME_CONTRACT_BASE_URL:-http://localhost:8000}"
RUNTIME_CONTRACT_TIMEOUT_SECONDS="${RUNTIME_CONTRACT_TIMEOUT_SECONDS:-120}"
require_runtime_tool() {
  local tool="$1"
  if [[ ! -x "runtime-tools/${tool}" ]]; then
    echo "Missing runtime-tools/${tool}" >&2
    echo "Build or copy prebuilt custom tool binaries before running smoke." >&2
    echo "See docs/CUSTOM_TOOLS_IMAGE.md" >&2
    exit 1
  fi
}
prepare_container_runtime() {
  if docker --version 2>/dev/null | grep -qi podman; then
    VOLUME_LABEL_OPT="Z"
  fi
}
volume_spec() {
  local host="$1"
  local container="$2"
  local options="${3:-}"
  if [[ -n "${VOLUME_LABEL_OPT}" ]]; then
    options="${options:+${options},}${VOLUME_LABEL_OPT}"
  fi
  if [[ -n "${options}" ]]; then
    printf "%s:%s:%s" "${host}" "${container}" "${options}"
  else
    printf "%s:%s" "${host}" "${container}"
  fi
}
prepare_maven_settings() {
  if [[ -f "${MAVEN_SETTINGS_PATH}" ]]; then
    return
  fi
  mkdir -p "${MAVEN_SETTINGS_DIR}"
  cat > "${MAVEN_SETTINGS_PATH}" <<'XML'
<?xml version="1.0" encoding="UTF-8"?>
<settings xmlns="http://maven.apache.org/SETTINGS/1.2.0"
          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
          xsi:schemaLocation="http://maven.apache.org/SETTINGS/1.2.0 https://maven.apache.org/xsd/settings-1.2.0.xsd">
</settings>
XML
  SMOKE_CREATED_MAVEN_SETTINGS=1
}
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
cleanup(){
  rm -f "${HEALTH_FILE}"||true
  rm -rf "${ASSET_ROOT}"||true
  if [[ "${SMOKE_CREATED_MAVEN_SETTINGS}" == "1" ]]; then
    rm -f "${MAVEN_SETTINGS_PATH}"||true
  fi
  docker rm -f "${NAME}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

require_runtime_tool jira
require_runtime_tool confluence
require_runtime_tool jenkins
require_runtime_tool aws-auth
require_runtime_tool mobile-auto
require_runtime_tool BrowserStackLocal
prepare_container_runtime
prepare_maven_settings
docker build --build-arg MAVEN_SETTINGS_DIR="${MAVEN_SETTINGS_DIR}" -t efp-opencode-runtime:test .
docker run -d --name "${NAME}" -p 8000:8000 -e OPENCODE_DATA_DIR=/root/.local/share/opencode -e EFP_ADAPTER_STATE_DIR=/root/.local/share/efp-compat -v "$(volume_spec "${WORKSPACE_DIR}" /workspace)" -v "$(volume_spec "${ADAPTER_STATE_DIR}" /root/.local/share/efp-compat)" -v "$(volume_spec "${OPENCODE_STATE_DIR}" /root/.local/share/opencode)" -v "$(volume_spec "${SKILLS_DIR}" /app/skills ro)" efp-opencode-runtime:test >/dev/null
for _ in $(seq 1 60); do curl -fsS http://localhost:8000/health >"${HEALTH_FILE}" && break || true; sleep 1; done
jq -e '.state.healthy == true' "${HEALTH_FILE}" >/dev/null
curl -fsS http://localhost:8000/api/skills | jq -e '.count >= 1' >/dev/null
curl -fsS http://localhost:8000/api/capabilities | jq -e '.engine == "opencode"' >/dev/null
if [[ "${RUN_RUNTIME_CONTRACT_TESTS}" == "1" ]]; then
  timeout "${RUNTIME_CONTRACT_TIMEOUT_SECONDS}" env "RUNTIME_BASE_URL=${RUNTIME_CONTRACT_BASE_URL}" python -m pytest -q runtime_contract_tests
fi
docker exec "${NAME}" bash -lc 'git --version && gh --version'
docker exec "${NAME}" bash -lc 'test -x /usr/local/bin/opencode-snapshot-recent-objects && test "$(git config --system --get gc.recentObjectsHook)" = "/usr/local/bin/opencode-snapshot-recent-objects"'
docker exec "${NAME}" bash -lc 'jira version --json >/dev/null && confluence version --json >/dev/null && jenkins version --json >/dev/null && aws-auth version --json >/dev/null && mobile-auto version --json >/dev/null && jira commands --json >/dev/null && jenkins commands --json >/dev/null && aws-auth commands --json >/dev/null && mobile-auto commands --json >/dev/null && jira schema issue.map-csv --json >/dev/null && jira schema issue.bulk-create --json >/dev/null && jenkins schema build.test-report --json >/dev/null && mobile-auto schema run.start --json >/dev/null && test -x /usr/local/bin/BrowserStackLocal'
docker exec "${NAME}" java -version
docker exec "${NAME}" javac -version
docker exec "${NAME}" mvn -v
docker exec "${NAME}" jdk list
docker exec "${NAME}" jdk current
docker exec "${NAME}" jdk 21 java -version
docker exec "${NAME}" mvn-jdk -v
docker exec "${NAME}" mvn-jdk 21 -v
docker exec "${NAME}" test -f /root/.m2/settings.xml
docker exec "${NAME}" test -f /root/.m2/toolchains.xml
docker exec "${NAME}" bash -lc 'test "$(stat -c %a /root/.m2/settings.xml)" = "600"'
docker exec "${NAME}" bash -lc 'test "$(stat -c %a /root/.m2/toolchains.xml)" = "600"'
echo "smoke passed"
