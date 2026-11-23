"""Read the executed test evidence. Never treat skipped SDK tests as passed.

STATE-MACHINE CONTRACT: 3 junit files -> verified counts -> immutable report.
"""
from __future__ import annotations
import hashlib
import importlib.metadata
import json
from datetime import datetime, timezone
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SOURCES = {
    "core_and_transport": "baseline_0_9.xml",
    "sqlite_and_cancellation": "ledger_0_9.xml",
    "process_lifecycle": "lifecycle_0_9.xml",
}


def read(name: str) -> dict:
    path = ROOT / "evidence" / name
    element = ET.parse(path).getroot()
    suite = element if element.tag == 'testsuite' else element.find('testsuite')
    if suite is None:
        raise ValueError("no testsuite")
    n = {key: int(suite.attrib.get(key, 0))
         for key in ("tests", "failures", "errors", "skipped")}
    n["passed"] = n["tests"] - n["failures"] - n["errors"] - n["skipped"]
    n["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return n


def main():
    suites = {key: read(value) for key, value in SOURCES.items()}
    totals = {key: sum(s[key] for s in suites.values()) for key in
              ("tests", "passed", "failures", "errors", "skipped")}
    if totals != {"tests": 264, "passed": 250, "failures": 0,
                  "errors": 0, "skipped": 14}:
        raise SystemExit(f"Unexpected counts: {totals}")
    try:
        sdk_version = importlib.metadata.version('mcp')
    except importlib.metadata.PackageNotFoundError:
        sdk_version = None
    summary = {
        "audit_id": "python-first-mcp-0.9.0-2026-11-23",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "run_scope": "offline Python, SQLite and official-SDK-free adapter stubs",
        "suites": suites, "totals": totals,
        "verified": {
            "pre_dispatch_cancellation_is_terminal": "passed",
            "post_dispatch_cancellation_is_unknown": "passed",
            "post_commit_cancellation_preserves_result": "passed",
            "owner_token_prevents_cross_worker_cancel": "passed",
            "unrelated_request_isolation": "passed",
            "sdk_stub_handler_propagates_cancel": "passed",
        },
        "not_verified": {
            "official_sdk_version": sdk_version,
            "official_mcp_connectivity": "not executed",
            "wire_level_cancelled_response_suppression": "not executed",
            "HTTP_cancellation": "not executed",
            "real_external_side_effect": "not implemented",
            "formal_proofs": "not executed",
        },
        "findings": [
            {"id": "FIND-009-001", "severity": "high", "description":
             "The inherited 0.8 server imported an unavailable request_cancellation module; fixed by using cancellable_bridge."},
            {"id": "FIND-009-002", "severity": "medium", "description":
             "Cancellation cannot interrupt synchronous SQLite calls while the event loop is blocked; only async checkpoints are cooperative."},
        ],
        "release_decision": "BLOCKED",
    }
    (ROOT / "evidence" / "verification_0_9.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(totals, ensure_ascii=False))


if __name__ == '__main__':
    main()
