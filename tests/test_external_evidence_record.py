"""Checks of the archived live run against real GitHub Issues.

The record was produced by scripts/run_github_issue_evidence.py in
.github/workflows/external-change-evidence.yml (run 45839952764). These tests
pin what that run observed to each durable_operations acceptance criterion.
"""
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RECORD = json.loads((ROOT / "evidence/external/github-issues-run-45839952764.json").read_text())
REPORT = RECORD["report"]
SCENARIOS = {scenario["name"]: scenario for scenario in REPORT["scenarios"]}
CONTRACT = json.loads((ROOT / REPORT["contract"]).read_text())


def observed(name):
    assert SCENARIOS[name]["passed"] and all(SCENARIOS[name]["checks"].values())
    return SCENARIOS[name]["observed"]


def test_live_run_passed_against_the_contracted_destination():
    assert REPORT["destination"] == CONTRACT["destination"] == "github_issues"
    assert REPORT["repository"] == "Anionix/vater_Tool-Execution-Runtime"
    assert REPORT["all_passed"] is True and set(SCENARIOS) == {"normal", "replay", "lost_reply", "crash", "target_check"}
    assert sorted(REPORT["closed"]) == sorted(REPORT["issues"])  # every evidence issue was closed
    assert RECORD["provenance"]["run_url"].endswith("/actions/runs/" + REPORT["run_id"])


@pytest.mark.parametrize("task", ["task.durable_operations.011", "task.durable_operations.012",
                                  "task.durable_operations.015", "task.durable_operations.016",
                                  "task.durable_operations.017", "task.durable_operations.021"])
def test_every_open_durable_task_has_a_passed_live_scenario(task):
    assert any(task in s["tasks"] and s["passed"] for s in REPORT["scenarios"])
    assert task in CONTRACT["evidence_status"]["required_before_tasks_close"]


def test_011_state_is_traceable_after_a_restart():
    crash = observed("crash")
    assert crash["crashed"] and crash["recovered_operations"] == 1 and crash["ledger_state"] == "unknown"
    assert observed("normal")["history"] == ["reserved", "dispatched", "definitive_succeeded"]


def test_012_the_result_is_queryable_by_operation_id():
    stored = observed("normal")["stored"]
    assert stored["status"] == "succeeded" and stored["object_id"] == str(observed("normal")["issue"])
    assert stored["operation_id"] and stored["destination"] == "github_issues"


def test_015_destination_deduplication_support_is_explicit():
    assert CONTRACT["idempotency"]["supported"] is False
    replay = observed("replay")
    assert replay["replayed"] and replay["new_posts"] == 0 and replay["issues_with_marker"] == 1


def test_016_unresolved_outcomes_go_to_a_person():
    assert observed("crash")["error"] == "HUMAN_RECONCILIATION_REQUIRED"
    assert observed("crash")["new_posts"] == 0 and observed("crash")["issues_with_marker"] == 0
    # The live list lagged once: the first lookup went to a person, a replay reconciled.
    assert observed("lost_reply")["first_store_status"] == "human_required"


def test_017_resends_are_limited():
    assert observed("replay")["new_posts"] == 0
    lost = observed("lost_reply")
    assert lost["posts"] == 1 and lost["posts_after_replay"] == 1 and lost["issues_with_marker"] == 1
    assert "rate-limit" in CONTRACT["retry_policy"]["resend_only_when"]


def test_021_target_swaps_are_detected_and_versions_recorded():
    normal = observed("normal")
    assert normal["stored"]["version"]["etag"] == normal["live_etag"]
    assert len(normal["stored"]["version"]["content_sha256"]) == 64
    target = observed("target_check")
    assert target == {"own": True, "other_operation": False, "other_title": False}
