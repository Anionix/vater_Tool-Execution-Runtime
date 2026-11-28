"""task.operations.017: the extended release is published only if the initial and every added-feature gate passed.

scripts/release_decision.py reads the required gates from the task ledger and
accepts a gate only when it is done and its evidence is executed, passed and
still bound to the current files. These tests build ledgers where everything
passes, where one added-feature gate is open, where one initial gate's
evidence went stale, and check that only the first allows the extended
release, and that the release job publishes it only on that decision.
"""
from __future__ import annotations

from hashlib import sha256
from importlib import util
import json
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = util.spec_from_file_location("release_decision", ROOT / "scripts" / "release_decision.py")
decision = util.module_from_spec(spec)
spec.loader.exec_module(decision)
WORKFLOW = (ROOT / ".github/workflows/python-verification.yml").read_text()
PHASES = {"scope": "python_minimum", "runtime": "python_minimum", "http_interface": "post_minimum",
          "testing": "post_minimum", "optional_webgpu": "post_minimum", "formal_verification": "optional_formal_rust"}


@pytest.fixture
def ledger(tmp_path):
    (tmp_path / "backlog").mkdir()
    (tmp_path / "evidence" / "gates").mkdir(parents=True)
    (tmp_path / "src.py").write_text("VALUE = 1\n")
    rows = []
    for phase, stage in PHASES.items():
        rows.append({"task_id": f"task.{phase}.001", "phase": phase, "implementation_stage": stage,
                     "status": "done", "evidence_paths": []})
        record = {"task_id": f"gate.{phase}.completed", "executed": True, "verification_status": "passed",
                  "source_sha256": {"src.py": sha256(b"VALUE = 1\n").hexdigest()}}
        (tmp_path / "evidence" / "gates" / f"{phase}.json").write_text(json.dumps(record))
        rows.append({"task_id": f"gate.{phase}.completed", "phase": phase, "implementation_stage": stage,
                     "status": "done", "evidence_paths": [f"evidence/gates/{phase}.json"]})
    (tmp_path / "backlog" / "tasks.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    return tmp_path


def rewrite(root: Path, gate: str, **changes) -> None:
    path = root / "backlog" / "tasks.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    path.write_text("".join(json.dumps({**row, **changes} if row["task_id"] == gate else row) + "\n" for row in rows))


def test_the_required_gates_come_from_the_ledger_and_skip_optional_phases():
    gates = decision.required_gates(decision.load_tasks())
    assert gates["initial"] == sorted(f"gate.{p}.completed" for p in (
        "scope", "trust", "contracts", "runtime", "mcp_transport", "authorization", "read_only",
        "minimum_validation", "minimum_release"))
    assert gates["added"] == sorted(f"gate.{p}.completed" for p in (
        "provider_adapters", "http_interface", "durable_operations", "testing", "operations"))


def test_every_gate_passing_with_fresh_evidence_allows_the_extended_release(ledger):
    result = decision.decide(ledger)
    assert result["initial_release"]["allowed"] is True and result["extended_release"]["allowed"] is True
    assert set(result["extended_release"]["added_gates"]) == {"gate.http_interface.completed",
                                                              "gate.testing.completed"}


def test_an_open_added_feature_gate_blocks_only_the_extended_release(ledger):
    rewrite(ledger, "gate.testing.completed", status="in_progress")
    result = decision.decide(ledger)
    assert result["initial_release"]["allowed"] is True
    assert result["extended_release"]["allowed"] is False
    assert result["extended_release"]["blocked_by"] == ["gate.testing.completed"]
    assert result["extended_release"]["added_gates"]["gate.testing.completed"]["reasons"] == ["gate is in_progress"]


def test_stale_initial_evidence_blocks_both_releases(ledger):
    (ledger / "src.py").write_text("VALUE = 2\n")  # every gate record is bound to it
    result = decision.decide(ledger)
    assert result["initial_release"]["allowed"] is False and result["extended_release"]["allowed"] is False
    assert result["initial_release"]["gates"]["gate.scope.completed"]["reasons"] == [
        "evidence/gates/scope.json is stale: src.py"]


def test_a_failed_or_missing_record_blocks_the_release(ledger):
    record = ledger / "evidence/gates/http_interface.json"
    record.write_text(json.dumps({**json.loads(record.read_text()), "verification_status": "failed"}))
    rewrite(ledger, "gate.runtime.completed", evidence_paths=[])
    result = decision.decide(ledger)
    assert result["extended_release"]["blocked_by"] == ["gate.http_interface.completed", "gate.runtime.completed"]


def test_the_repository_decision_names_a_reason_for_every_blocked_gate(tmp_path, capsys):
    output, github = tmp_path / "decision.json", tmp_path / "github_output"
    assert decision.main(["--output", str(output), "--github-output", str(github)]) == 0
    result = json.loads(output.read_text())
    gates = {**result["initial_release"]["gates"], **result["extended_release"]["added_gates"]}
    assert all(gate["reasons"] for name, gate in gates.items() if name in result["extended_release"]["blocked_by"])
    extended = str(result["extended_release"]["allowed"]).lower()
    assert github.read_text().endswith(f"extended={extended}\n")
    if not result["extended_release"]["allowed"]:
        assert decision.main(["--output", str(output), "--require", "extended"]) == 1


def test_the_release_job_publishes_the_extended_release_only_on_the_decision():
    job = re.split(r"\n  (?=[A-Za-z0-9_-]+:\n)", WORKFLOW.split("  release-artifacts:\n", 1)[1], maxsplit=1)[0]
    decide = job.index("python scripts/release_decision.py --output release-decision.json")
    publish = job.split("- name: Publish the extended release", 1)[1].split("- name:", 1)[0]
    assert job.index("- name: Publish the extended release") > decide
    assert "if: steps.decision.outputs.extended == 'true'" in publish and "name: extended-release" in publish
