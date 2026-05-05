#!/usr/bin/env bash
set -euo pipefail

PYTEST_TIMEOUT_SECONDS="${PYTEST_TIMEOUT_SECONDS:-240}"
FULL_PYTEST_TIMEOUT_SECONDS="${FULL_PYTEST_TIMEOUT_SECONDS:-360}"
PYTEST_KILL_AFTER_SECONDS="${PYTEST_KILL_AFTER_SECONDS:-10}"
CI_LOG_DIR="${CI_LOG_DIR:-/tmp/efp-opencode-ci-logs}"

mkdir -p "${CI_LOG_DIR}"

run_pytest() {
  local timeout_seconds="$1"
  shift
  echo "+ timeout --kill-after=${PYTEST_KILL_AFTER_SECONDS} ${timeout_seconds} python -m pytest $*"

  set +e
  timeout --kill-after="${PYTEST_KILL_AFTER_SECONDS}" "${timeout_seconds}" python -m pytest "$@"
  local status="$?"
  set -e

  if [[ "${status}" -ne 0 ]]; then
    echo "pytest gate failed or timed out with status ${status}: python -m pytest $*" >&2
  fi
  return "${status}"
}

_slug() {
  echo "$1" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' | sed 's/^-//; s/-$//'
}

run_pytest_gate() {
  local label="$1"
  local timeout_seconds="$2"
  shift 2

  local slug
  slug="$(_slug "${label}")"
  local log_file="${CI_LOG_DIR}/${slug}.log"

  echo "== ${label} =="
  echo "log: ${log_file}"

  set +e
  run_pytest "${timeout_seconds}" "$@" >"${log_file}" 2>&1
  local status="$?"
  set -e

  cat "${log_file}"

  if [[ "${status}" -ne 0 ]]; then
    echo "== ${label} failed; tail of ${log_file} ==" >&2
    tail -120 "${log_file}" >&2 || true
  fi

  return "${status}"
}

run_pytest_gate "opencode_client leak gate" "${PYTEST_TIMEOUT_SECONDS}" -q tests/test_opencode_client.py
OPENCODE_CLIENT_LOG="${CI_LOG_DIR}/opencode-client-leak-gate.log"
! grep -q "Unclosed client session" "${OPENCODE_CLIENT_LOG}"
! grep -q "Unclosed connector" "${OPENCODE_CLIENT_LOG}"
! grep -q "PytestUnraisableExceptionWarning" "${OPENCODE_CLIENT_LOG}"

run_pytest_gate "AppKey static/runtime gate" "${PYTEST_TIMEOUT_SECONDS}" -q tests/test_app_keys.py
! grep -R 'from \.app_keys import \*' -n efp_opencode_adapter
! grep -R 'app.get("' -n efp_opencode_adapter
! grep -R "app.get('" -n efp_opencode_adapter
! grep -R 'request.app.get("' -n efp_opencode_adapter
! grep -R "request.app.get('" -n efp_opencode_adapter
! grep -R 'app\["' -n efp_opencode_adapter
! grep -R "app\['" -n efp_opencode_adapter
! grep -R 'request.app\["' -n efp_opencode_adapter
! grep -R "request.app\['" -n efp_opencode_adapter

run_pytest_gate "pytest config gate" "${PYTEST_TIMEOUT_SECONDS}" -q tests/test_pytest_config.py tests/test_pytest_plugin_config.py
run_pytest_gate "runtime contract default skip/import gate" "${PYTEST_TIMEOUT_SECONDS}" -q runtime_contract_tests
run_pytest_gate "NotAppKeyWarning gate" "${PYTEST_TIMEOUT_SECONDS}" -q tests/test_app_keys.py -W error::aiohttp.web_exceptions.NotAppKeyWarning

echo "== P2 subset gates =="
run_pytest_gate "P2 chat stream/recovery subset" "${PYTEST_TIMEOUT_SECONDS}" -q \
  tests/test_chat_streaming.py \
  tests/test_chat_recovery_hardening.py

run_pytest_gate "P2 skill/assets/capabilities subset" "${PYTEST_TIMEOUT_SECONDS}" -q \
  tests/test_skill_sync.py \
  tests/test_capabilities_api.py \
  tests/test_init_assets.py

run_pytest_gate "P2 event/profile subset" "${PYTEST_TIMEOUT_SECONDS}" -q \
  tests/test_event_bridge.py \
  tests/test_runtime_profile_apply.py

run_pytest_gate "P2 tasks/recovery/tools subset" "${PYTEST_TIMEOUT_SECONDS}" -q \
  tests/test_tasks_api.py \
  tests/test_recovery.py \
  tests/test_tool_sync.py

run_pytest_gate "full pytest" "${FULL_PYTEST_TIMEOUT_SECONDS}" -q
