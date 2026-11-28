#!/usr/bin/env python3
"""task.operations.005: scan every locked dependency for known vulnerabilities and keep the result.

  python -I scripts/scan_vulnerabilities.py --output osv-scan.json      # live scan (network: api.osv.dev)
  python scripts/scan_vulnerabilities.py --check-record evidence/security/osv-scan.json   # offline

The live scan sends one OSV querybatch with the name and exact version of each
package in requirements-py313.lock (standard library only, no third-party
scanner is installed) and writes a report with the advisory ids found per
package. Exit status 1 when any package has a known vulnerability or the
query fails. --check-record verifies, offline, that a saved report covers the
current lock exactly and found nothing, so the saved result cannot silently
describe another dependency set.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "requirements-py313.lock"
ENDPOINT = "https://api.osv.dev/v1/querybatch"


def locked_packages(lock: Path = LOCK) -> list[tuple[str, str]]:
    found = []
    for line in lock.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)==(\S+)", line)
        if match:
            found.append((re.sub(r"[-_.]+", "-", match.group(1)).lower(), match.group(2)))
    return sorted(found)


def lock_digest(lock: Path = LOCK) -> str:
    return hashlib.sha256(lock.read_bytes()).hexdigest()


def query(packages: list[tuple[str, str]], post=None) -> list[list[str]]:
    body = json.dumps({"queries": [{"package": {"name": name, "ecosystem": "PyPI"}, "version": version}
                                   for name, version in packages]}).encode()
    if post is None:
        def post(data: bytes) -> dict:
            request = urllib.request.Request(ENDPOINT, data=data, method="POST",
                                             headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=60) as reply:
                return json.loads(reply.read())
    results = post(body)["results"]
    if len(results) != len(packages):
        raise ValueError("OSV answered a different number of queries")
    return [sorted(vuln["id"] for vuln in result.get("vulns", [])) for result in results]


def report(packages: list[tuple[str, str]], ids: list[list[str]], lock_sha256: str,
           lock_name: str = "requirements-py313.lock") -> dict:
    rows = [{"name": name, "version": version, "vulnerabilities": found}
            for (name, version), found in zip(packages, ids)]
    return {"source": ENDPOINT, "ecosystem": "PyPI",
            "scanned_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "lock": lock_name, "lock_sha256": lock_sha256, "packages": rows,
            "vulnerable_packages": sum(1 for row in rows if row["vulnerabilities"])}


def canonical_sha256(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def check_record(record: dict, lock: Path = LOCK) -> list[str]:
    problems = []
    if record.get("lock_sha256") != lock_digest(lock):
        problems.append("the saved scan is for another lock")
    if [(row["name"], row["version"]) for row in record.get("packages", [])] != locked_packages(lock):
        problems.append("the saved scan does not list exactly the locked packages")
    vulnerable = [row["name"] for row in record.get("packages", []) if row["vulnerabilities"]]
    if vulnerable or record.get("vulnerable_packages"):
        problems.append("known vulnerabilities: " + ", ".join(vulnerable))
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output", type=Path)
    mode.add_argument("--check-record", type=Path)
    # The optional prototypes' lock (task.optional_*) is scanned the same way as the base lock.
    parser.add_argument("--lock", choices=["requirements-py313.lock", "requirements-optional-py313.lock"],
                        default="requirements-py313.lock")
    args = parser.parse_args(argv)
    lock = ROOT / args.lock
    if args.check_record is not None:
        problems = check_record(json.loads(args.check_record.read_text(encoding="utf-8")), lock)
        print(json.dumps({"record": str(args.check_record), "problems": problems}))
        return 1 if problems else 0
    packages = locked_packages(lock)
    try:
        found = query(packages)
    except (OSError, ValueError, KeyError) as error:
        print(json.dumps({"error": f"scan failed: {type(error).__name__}: {error}"}), file=sys.stderr)
        return 1
    result = report(packages, found, lock_digest(lock), args.lock)
    text = json.dumps(result, indent=1)
    args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    print(f"OSV_REPORT_CANONICAL_SHA256={canonical_sha256(result)}")
    return 1 if result["vulnerable_packages"] else 0


if __name__ == "__main__":
    sys.exit(main())
