"""Run the official SDK transport integration suite and record truthful status.

Exit codes:
  0 - SDK installed, all real SDK tests passed.
  1 - real SDK tests failed.
  2 - SDK v2 unavailable, tests not run.
"""
from __future__ import annotations

import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence"
EVIDENCE.mkdir(exist_ok=True)


def main() -> int:
    result = {"test_suite": "official_mcp_python_sdk_v2", "timestamp_utc": datetime.now(timezone.utc).isoformat(), "status": "unverified"}
    try:
        installed_version = importlib.metadata.version("mcp")
    except importlib.metadata.PackageNotFoundError:
        installed_version = None
    result["installed_sdk_version"] = installed_version
    if installed_version is None or not installed_version.startswith("2."):
        result.update(status="blocked_dependency", reason="official MCP Python SDK 2.x not installed", executed_cases=0, passed=0, failed=0)
        (EVIDENCE / "sdk_e2e_result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False))
        return 2

    cmd = [sys.executable, "-m", "pytest", "-q", "-rs", "tests/test_official_mcp_sdk.py", "--junitxml=evidence/sdk_e2e_junit.xml"]
    try:
        run = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=90)
        result["exit_code"] = run.returncode
        result["stdout"] = run.stdout[-4000:]
        result["stderr"] = run.stderr[-4000:]
        junit = EVIDENCE / "sdk_e2e_junit.xml"
        if junit.exists():
            root = ET.parse(junit).getroot()
            suite = root.find("testsuite") if root.tag != "testsuite" else root
            if suite is not None:
                count = int(suite.attrib.get("tests", 0))
                fails = int(suite.attrib.get("failures", 0)) + int(suite.attrib.get("errors", 0))
                skipped = int(suite.attrib.get("skipped", 0))
                result.update(executed_cases=count-skipped, passed=count-skipped-fails, failed=fails, skipped=skipped)
        result["status"] = "passed" if run.returncode == 0 and result.get("skipped", 0) == 0 else "failed_or_incomplete"
    except subprocess.TimeoutExpired:
        result.update(status="timeout", reason="SDK test suite exceeded 90 seconds", passed=0, failed=1)
    (EVIDENCE / "sdk_e2e_result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({k:v for k,v in result.items() if k not in {"stdout", "stderr"}}, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
