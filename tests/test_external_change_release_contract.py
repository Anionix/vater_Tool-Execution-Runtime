"""Keep the external change release requirements tied to the task ledger."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_external_change_release_requires_all_gates_and_service_evidence():
    contract = json.loads((ROOT / "contracts/external_change_release.json").read_text())
    profiles = json.loads((ROOT / "docs/RELEASE_PROFILES.json").read_text())
    rows = (ROOT / "backlog/tasks.jsonl").read_text().splitlines()
    tasks = {task["task_id"]: task for task in map(json.loads, rows)}

    profile = profiles[contract["release_profile"]]
    assert profile["extends"] == "minimum_local_read_only"
    required_gates = {
        "gate.minimum_release.completed",
        "gate.durable_operations.completed",
        "gate.testing.completed",
        "gate.operations.completed",
    }
    assert set(contract["required_completed_gates"]) == required_gates
    assert {
        f"gate.{phase}.completed" for phase in profile["gates"]
    } == required_gates - {"gate.minimum_release.completed"}

    # task.durable_operations.023: missing service evidence -> blocked;
    # verified release gates and service evidence -> eligible for release review.
    for identifier in contract["required_completed_gates"]:
        assert identifier in tasks
    durable_gate = tasks["gate.durable_operations.completed"]
    service_evidence = set(contract["required_service_evidence"])
    assert service_evidence == {
        f"task.durable_operations.{number:03d}" for number in (11, 12, 15, 16, 17, 21)
    }
    assert service_evidence | {contract["task_id"]} <= set(durable_gate["depends_on"])
    assert all(description.strip() for description in contract["required_service_evidence"].values())
