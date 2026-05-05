#!/usr/bin/env bash
set -euo pipefail

echo "== opencode_client leak gate =="
python -m pytest -q tests/test_opencode_client.py 2>&1 | tee /tmp/opencode-client.log
! grep -q "Unclosed client session" /tmp/opencode-client.log
! grep -q "Unclosed connector" /tmp/opencode-client.log
! grep -q "PytestUnraisableExceptionWarning" /tmp/opencode-client.log

echo "== AppKey static/runtime gate =="
python -m pytest -q tests/test_app_keys.py
! grep -R 'from \.app_keys import \*' -n efp_opencode_adapter
! grep -R 'app.get("' -n efp_opencode_adapter
! grep -R 'request.app.get("' -n efp_opencode_adapter
! grep -R 'app\["' -n efp_opencode_adapter
! grep -R 'request.app\["' -n efp_opencode_adapter

echo "== pytest config gate =="
python -m pytest -q tests/test_pytest_config.py

echo "== NotAppKeyWarning gate =="
python -m pytest -q tests/test_app_keys.py -W error::aiohttp.web_exceptions.NotAppKeyWarning

echo "== P2 subset =="
python -m pytest -q \
  tests/test_chat_streaming.py \
  tests/test_chat_recovery_hardening.py \
  tests/test_skill_sync.py \
  tests/test_capabilities_api.py \
  tests/test_init_assets.py \
  tests/test_event_bridge.py \
  tests/test_runtime_profile_apply.py \
  tests/test_tasks_api.py \
  tests/test_recovery.py \
  tests/test_tool_sync.py

echo "== full pytest =="
python -m pytest -q
