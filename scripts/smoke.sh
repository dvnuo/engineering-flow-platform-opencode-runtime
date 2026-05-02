#!/usr/bin/env bash
set -euo pipefail

NAME="efp-opencode-runtime-smoke"
HEALTH_FILE="$(mktemp)"
cleanup() {
  rm -f "${HEALTH_FILE}" >/dev/null 2>&1 || true
  docker rm -f "${NAME}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker build -t efp-opencode-runtime:test .
docker run -d --name "${NAME}" -p 8000:8000 -e OPENCODE_SERVER_PASSWORD=test-password efp-opencode-runtime:test >/dev/null

READY=0
for _ in $(seq 1 60); do
  if curl -fsS http://localhost:8000/health >"${HEALTH_FILE}"; then
    READY=1
    break
  fi
  sleep 1
done

if [[ "${READY}" != "1" ]]; then
  docker logs "${NAME}" >&2 || true
  echo "health endpoint did not become ready within 60 seconds" >&2
  exit 1
fi

jq -e '.status == "ok"' "${HEALTH_FILE}" >/dev/null
jq -e '.engine == "opencode"' "${HEALTH_FILE}" >/dev/null
jq -e '.opencode_version == "1.14.29"' "${HEALTH_FILE}" >/dev/null

LOGS="$(docker logs "${NAME}" 2>&1)"
[[ "${LOGS}" == *"adapter listening on 0.0.0.0:8000"* ]]
[[ "${LOGS}" == *"opencode serve listening on 127.0.0.1:4096"* ]]
[[ "${LOGS}" == *"opencode version"* ]]

docker exec "${NAME}" bash -lc 'test "$(id -u)" = "10001"'
docker exec "${NAME}" test -d /workspace/.opencode/skills
docker exec "${NAME}" test -d /workspace/.opencode/tools
docker exec "${NAME}" test -d /workspace/.opencode/agents
docker exec "${NAME}" test -f /workspace/.opencode/opencode.json
docker exec "${NAME}" jq -e '.autoupdate == false' /workspace/.opencode/opencode.json >/dev/null
docker exec "${NAME}" jq -e '.share == "disabled"' /workspace/.opencode/opencode.json >/dev/null
docker exec "${NAME}" jq -e '.permission["*"] == "ask"' /workspace/.opencode/opencode.json >/dev/null
docker exec "${NAME}" jq -e '.permission.external_directory == "deny"' /workspace/.opencode/opencode.json >/dev/null
docker exec "${NAME}" bash -lc 'opencode --version | grep -F "1.14.29"'

echo "smoke passed"
