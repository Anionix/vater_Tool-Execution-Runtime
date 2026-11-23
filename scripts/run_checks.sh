#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python -m pytest -q -rs --junitxml=evidence/python_junit.xml
python scripts/run_sdk_e2e.py
