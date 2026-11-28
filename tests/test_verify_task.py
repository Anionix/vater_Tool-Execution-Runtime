"""task.operations.003: one change is one independent check unit.

scripts/verify_task.py checks one task by itself: its single evidence record,
exactly its pytest cases in a new process, and its review scope (the files
bound to the record). These tests run real tasks alone, require a failing or
unmapped task to fail without borrowing other tests, and require every done
task of the verified phases to be such a self-contained unit.
"""
from __future__ import annotations

from importlib import util
import json
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = util.spec_from_file_location("verify_task", ROOT / "scripts" / "verify_task.py")
verify_task = util.module_from_spec(spec)
spec.loader.exec_module(verify_task)
FAIL_ON_PURPOSE = "VATER_VERIFY_TASK_FAIL_ON_PURPOSE"


def test_helper_that_fails_only_when_asked():
    assert os.environ.get(FAIL_ON_PURPOSE) != "1"


@pytest.mark.parametrize("task_id", ["task.http_interface.003", "task.authorization.001"])
def test_a_task_is_checked_alone_with_exactly_its_cases(task_id, capsys):
    cases = verify_task.task_cases(verify_task.load_tasks(), task_id)
    assert verify_task.main([task_id]) == 0
    result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert result["task_id"] == task_id and result["tasks"] == 1
    assert result["required_cases"] == len(cases) and result["report"]["passed"] == len(cases)
    assert result["report"]["failed"] == result["report"]["skipped"] == result["report"]["errors"] == 0
    assert result["review_scope"] and set(result["review_scope"].values()) <= {"unchanged", "changed", "missing"}


def test_a_failing_case_fails_its_unit(monkeypatch, capsys):
    monkeypatch.setenv(FAIL_ON_PURPOSE, "1")
    monkeypatch.setattr(verify_task, "task_cases", lambda tasks, key: [
        "tests/test_verify_task.py::test_helper_that_fails_only_when_asked"])
    assert verify_task.main(["task.http_interface.003"]) == 1


def test_unknown_gate_or_unready_tasks_are_refused_before_anything_runs(monkeypatch):
    ran = []
    monkeypatch.setattr(verify_task.subprocess, "run", lambda *a, **k: ran.append(a))
    assert verify_task.main(["task.does.not.exist"]) == 1
    assert verify_task.main(["gate.testing.completed"]) == 1
    tasks = verify_task.load_tasks()
    parent = tasks["task.http_interface.003"]["depends_on"][0]
    tasks[parent] = {**tasks[parent], "status": "in_progress"}
    monkeypatch.setattr(verify_task, "load_tasks", lambda: tasks)
    assert verify_task.main(["task.http_interface.003"]) == 1
    assert ran == []


def test_every_done_task_of_the_verified_phases_is_a_self_contained_unit():
    tasks = verify_task.load_tasks()
    done = [key for key, row in tasks.items() if key.startswith("task.") and row["status"] == "done"]
    units = {key: verify_task.task_cases(tasks, key) for key in done if tasks[key]["phase"] != "scope"}
    assert len(units) >= 240 and all(units.values())
    # The scope phase predates case lists (its records use older formats); each is still one record.
    for key in done:
        if tasks[key]["phase"] == "scope":
            assert len(tasks[key]["evidence_paths"]) == 1
            assert json.loads((ROOT / tasks[key]["evidence_paths"][0]).read_text())["executed"] is True
