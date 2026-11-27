"""task.testing.025: time, resources and cost per operation, with a baseline and a diff.

evidence/performance/baseline.json was measured on main before the testing
phase; evidence/performance/diff-testing-phase.json compares this tree with
it (scripts/measure_operations.py). Timings depend on the machine, so these
tests check that both records exist and are complete, that the comparison
flags regressions, and that the deterministic costs (requests to external
services and paid API calls per operation) measured now equal the baseline.
"""
from __future__ import annotations

import hashlib
from importlib import util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = util.spec_from_file_location("measure_operations", ROOT / "scripts" / "measure_operations.py")
measure = util.module_from_spec(spec)
spec.loader.exec_module(measure)
BASELINE_PATH = ROOT / "evidence" / "performance" / "baseline.json"
DIFF_PATH = ROOT / "evidence" / "performance" / "diff-testing-phase.json"
BASELINE = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
METRICS = ("wall_us", "cpu_us", "peak_rss_kib", "ledger_bytes_per_operation",
           "external_requests_per_operation", "paid_api_calls_per_operation")


def test_baseline_is_complete():
    assert BASELINE["kind"] == "operation_costs" and BASELINE["source"]["commit"]
    assert {"python", "platform", "machine", "cpu_count"} <= set(BASELINE["environment"])
    assert set(BASELINE["operations"]) == set(measure.OPERATIONS)
    for operation, values in BASELINE["operations"].items():
        assert set(METRICS) <= set(values), operation
        assert 0 < values["wall_us"]["median"] <= values["wall_us"]["p95"] <= values["wall_us"]["max"]
        assert values["paid_api_calls_per_operation"] == 0


def test_diff_is_recorded_against_this_baseline():
    diff = json.loads(DIFF_PATH.read_text(encoding="utf-8"))
    assert diff["baseline_sha256"] == hashlib.sha256(BASELINE_PATH.read_bytes()).hexdigest()
    assert diff["baseline_source"] == BASELINE["source"] and diff["candidate_source"]["commit"]
    assert set(diff["changes"]) == set(measure.OPERATIONS)
    for operation, changes in diff["changes"].items():
        assert set(changes) == {*measure.THRESHOLDS, *measure.EXACT}, operation
    assert isinstance(diff["regressions"], list)


@pytest.mark.parametrize("operation", measure.OPERATIONS)
def test_deterministic_costs_match_the_baseline(operation):
    if operation == "mcp_call":
        pytest.importorskip("mcp")
    # SQLite grows the file in 4 KiB pages: forty operations always add at least one page.
    now = measure.run_operation(operation, 40, ROOT)
    base = BASELINE["operations"][operation]
    for name in measure.EXACT:
        assert now[name] == base[name], (operation, name)
    assert (now["ledger_bytes_per_operation"] > 0) == (base["ledger_bytes_per_operation"] > 0)


def test_comparison_flags_regressions_and_exact_cost_changes():
    def record(wall, requests):
        values = {"wall_us": {"median": wall}, "cpu_us": {"median": wall}, "peak_rss_kib": 100,
                  "ledger_bytes_per_operation": 0, "external_requests_per_operation": requests,
                  "paid_api_calls_per_operation": 0}
        return {"operations": {"op": values}}

    assert measure.compare(record(100, 2), record(110, 2))["regressions"] == []
    slower = measure.compare(record(100, 2), record(200, 2))["regressions"]
    assert {item["metric"] for item in slower} == {"wall_us.median", "cpu_us.median"}
    costlier = measure.compare(record(100, 2), record(100, 3))["regressions"]
    assert [item["metric"] for item in costlier] == ["external_requests_per_operation"]
    assert measure.compare({"operations": {}}, record(1, 0))["regressions"][0]["reason"] == "no baseline"
