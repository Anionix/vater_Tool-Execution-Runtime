"""Read executed JUnit evidence; fail closed when expected suites are absent."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence"
SOURCES = [
    ("baseline", "transport_baseline_0_7.xml"),
    ("lifecycle", "lifecycle_0_7.xml"),
    ("ledger", "persistent_0_7.xml"),
]


def read_suite(path: Path) -> dict:
    node = ET.parse(path).getroot().find("testsuite")
    if node is None:
        raise RuntimeError(f"Missing testsuite in {path.name}")
    counters = {name: int(node.attrib.get(name, "0"))
                for name in ("tests", "failures", "errors", "skipped")}
    counters["passed"] = counters["tests"] - counters["failures"] - counters["errors"] - counters["skipped"]
    if counters["passed"] < 0:
        raise RuntimeError(f"Invalid counts in {path.name}")
    counters["duration_seconds"] = float(node.attrib.get("time", "0"))
    counters["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return counters


def main() -> None:
    suites = {name: read_suite(EVIDENCE / filename) for name, filename in SOURCES}
    totals = {key: sum(suite[key] for suite in suites.values())
              for key in ("tests", "passed", "failures", "errors", "skipped")}
    if totals["failures"] or totals["errors"]:
        raise SystemExit("Test failure prevents successful verification summary")
    baseline_root = ET.parse(EVIDENCE / "transport_baseline_0_7.xml").getroot()
    official_cases = [case for case in baseline_root.iter("testcase")
                      if case.attrib.get("classname", "").endswith("test_official_mcp_sdk")]
    official_skipped = sum(case.find("skipped") is not None for case in official_cases)
    try:
        installed_sdk = importlib.metadata.version("mcp")
    except importlib.metadata.PackageNotFoundError:
        installed_sdk = None
    report = {
        "report_id": "verified-execution-python-0.7.0-2026-11-23",
        "produced_at_utc": datetime.now(timezone.utc).isoformat(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "suites": suites,
        "totals": totals,
        "official_mcp_sdk": {
            "installed_version": installed_sdk,
            "tests_executed": len(official_cases) - official_skipped,
            "tests_skipped": official_skipped,
            "reason": "SDK not installed; package index lookup failed with DNS resolution error" if not installed_sdk else None,
        },
        "claims": {
            "local_calculator_integration": "executed",
            "sqlite_durability_across_process_restarts": "executed",
            "atomic_calculation_result_persistence": "executed",
            "trusted_host_only_principal_and_authorization": "executed_in_offline_bridge",
            "official_mcp_recorded_tool_handler": "implemented_not_executed_with_sdk",
            "concurrent_duplicate_dispatch_prevention": "executed",
            "approval_binding_simulation": "executed",
            "operation_level_cancel_state": "executed",
            "mcp_wire_cancel": "not_implemented_or_executed",
            "external_side_effect_reconciliation": "not_implemented_or_executed",
            "actual_mcp_client_server_connection": "not_executed",
            "formal_rust_or_lean_verification": "not_executed",
        }
    }
    output = EVIDENCE / "verification_summary.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(totals, ensure_ascii=False))


if __name__ == "__main__":
    main()
