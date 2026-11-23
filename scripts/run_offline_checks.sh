#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
python -m compileall -q src scripts server_mcp.py tests
python -m pytest -q -rs --junitxml=evidence/baseline_0_9.xml \
  tests/test_adapter_boundary.py tests/test_cli.py tests/test_contracts.py \
  tests/test_core.py tests/test_guarded_stdio.py tests/test_output_boundary.py \
  tests/test_official_mcp_sdk.py
python -m pytest -q --junitxml=evidence/ledger_0_9.xml \
  tests/test_ledger.py tests/test_recorded_calculator.py tests/test_recorded_cli.py \
  tests/test_recorded_tool_bridge.py tests/test_worker_leases.py \
  tests/test_worker_bridge.py tests/test_request_cancellation.py
python -m pytest -q --junitxml=evidence/lifecycle_0_9.xml \
  tests/test_lifecycle.py tests/test_process_lifecycle.py
python scripts/report_0_9.py
