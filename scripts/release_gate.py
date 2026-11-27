#!/usr/bin/env python3
"""task.operations.009: a failed security check stops the release.

  python scripts/release_gate.py --output release-gate.json

Runs every security check a release depends on, all of them even after a
failure, and allows the release only if each one passed:
  static_policy             production-code static safety policy
  permission_history        no permission change without its history entry
  secret_audit              no secret value in evidence or documentation
  saved_vulnerability_scan  the saved OSV scan covers this lock and found nothing
  live_vulnerability_scan   a fresh OSV scan of every locked package finds nothing
The release job runs this before building and publishes artifacts only when
it exits 0; it also needs every verification job (including the safety tests)
to have passed first.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
CHECKS = {
    "static_policy": ["scripts/check_static_policy.py"],
    "permission_history": ["scripts/permission_history.py", "check"],
    "secret_audit": ["scripts/audit_logs.py", "evidence/", "docs/"],
    "saved_vulnerability_scan": ["scripts/scan_vulnerabilities.py", "--check-record", "evidence/security/osv-scan.json"],
    "live_vulnerability_scan": ["-I", "scripts/scan_vulnerabilities.py", "--output", "osv-scan.json"],
}


def run_check(arguments: list[str]) -> int:
    return subprocess.run([sys.executable, *arguments], cwd=ROOT, capture_output=True, text=True).returncode


def gate(checks: dict[str, list[str]] = CHECKS, runner=run_check) -> dict:
    results = {name: {"returncode": runner(arguments)} for name, arguments in checks.items()}
    for result in results.values():
        result["passed"] = result["returncode"] == 0
    failed = sorted(name for name, result in results.items() if not result["passed"])
    return {"checks": results, "failed": failed, "release_allowed": bool(results) and not failed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = gate()
    text = json.dumps(report, indent=1)
    args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if report["release_allowed"] else 1


if __name__ == "__main__":
    sys.exit(main())
