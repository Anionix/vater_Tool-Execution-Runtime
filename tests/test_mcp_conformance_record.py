"""task.testing.022: the official MCP conformance results are saved per protocol version.

The suite itself runs only in CI (.github/workflows/mcp-conformance.yml); its
results are committed in evidence/mcp-conformance/results.json and every CI
run is compared with them by scripts/check_mcp_conformance.py. These tests
check the record offline: one entry per version the SDK supports, pinned to
the workflow's upstream commit, every scenario classified, the required ones
passed; and the checker rejects a regression, a wire error, a drift from the
record, an unclassified scenario and another upstream commit.
"""
from __future__ import annotations

import copy
from importlib import util
import json
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = util.spec_from_file_location("check_mcp_conformance", ROOT / "scripts" / "check_mcp_conformance.py")
checker = util.module_from_spec(spec)
spec.loader.exec_module(checker)
RECORD = json.loads(checker.RECORD.read_text(encoding="utf-8"))
POLICY = json.loads(checker.POLICY.read_text(encoding="utf-8"))
WORKFLOW = (ROOT / ".github" / "workflows" / "mcp-conformance.yml").read_text(encoding="utf-8")


def as_run(record):
    """The record rebuilt as the per-version files the runner writes."""
    return {version: {"specVersion": version, "upstream": record["upstream"],
                      "scenarios": [{"name": name, "checks": [{"id": i, "status": s} for i, s in checks],
                                     "wireErrors": []} for name, checks in scenarios.items()]}
            for version, scenarios in record["versions"].items()}


def test_one_result_per_supported_protocol_version():
    assert set(RECORD["versions"]) == {"2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28"}
    assert RECORD["executed"] is True and RECORD["verification_status"] == "passed"
    assert re.fullmatch(r"[0-9a-f]{64}", RECORD["run"]["artifact_zip_sha256"])


def test_record_is_pinned_to_the_workflow_upstream_commit():
    assert f"UPSTREAM_COMMIT: {RECORD['upstream']['commit']}" in WORKFLOW
    assert "python scripts/check_mcp_conformance.py mcp-conformance" in WORKFLOW
    assert "permissions:\n  contents: read\n" in WORKFLOW and "--ignore-scripts" in WORKFLOW


def test_every_scenario_is_classified_and_the_required_ones_passed():
    for version, observed in RECORD["versions"].items():
        assert checker.policy_problems(version, observed, POLICY) == []
    required = {name for name, rule in POLICY["scenarios"].items() if rule.get("required")}
    assert required == {"ping", "tools-list", "tools-call-simple-text", "tools-call-error"}
    for version in ("2025-11-25", "2026-07-28"):
        assert {"tools-list", "tools-call-simple-text", "tools-call-error"} <= set(RECORD["versions"][version])


def test_the_record_passes_its_own_check():
    assert checker.run_problems(as_run(RECORD), RECORD, POLICY) == []


@pytest.mark.parametrize("change", ["regression", "wire_error", "drift", "unclassified", "other_commit"])
def test_the_checker_rejects(change):
    run = as_run(RECORD)
    result = run["2026-07-28"]
    if change == "regression":
        next(s for s in result["scenarios"] if s["name"] == "tools-call-error")["checks"][0]["status"] = "FAILURE"
    elif change == "wire_error":
        result["scenarios"][0]["wireErrors"] = ["response tools/list: /result/tools: must be array"]
    elif change == "drift":
        next(s for s in result["scenarios"] if s["name"] == "prompts-list")["checks"][0]["status"] = "SUCCESS"
    elif change == "unclassified":
        result["scenarios"].append({"name": "brand-new-scenario", "checks": [{"id": "x", "status": "SUCCESS"}],
                                    "wireErrors": []})
    else:
        result["upstream"] = {**result["upstream"], "commit": "0" * 40}
    assert checker.run_problems(run, copy.deepcopy(RECORD), POLICY) != []
