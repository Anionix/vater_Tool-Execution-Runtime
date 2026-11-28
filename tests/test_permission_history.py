"""task.operations.013: configuration changes are recorded, and permission changes leave a history.

docs/permission_history.jsonl is a hash chain of permission snapshots (tool
risk classes, the launcher's approved code, external destinations, limits and
workflow token permissions) with the changes between them. These tests
require the repository's history to be intact and current, a permission
change to fail the check until it is recorded with a reason and an approver,
an edited history to be detected, and past approvals to appear in the history.
"""
from __future__ import annotations

from importlib import util
import json
from pathlib import Path
import shutil

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = util.spec_from_file_location("permission_history", ROOT / "scripts" / "permission_history.py")
history = util.module_from_spec(spec)
spec.loader.exec_module(history)
FILES = ["contracts/tool_risks.json", "config/local_tool.json", "config/limits.json",
         "contracts/github_issue_destination.json", "docs/permission_history.jsonl",
         *(f".github/workflows/{p.name}" for p in (ROOT / ".github/workflows").glob("*.yml"))]


@pytest.fixture
def copy(tmp_path):
    for relative in FILES:
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, tmp_path / relative)
    return tmp_path


def edit_json(path: Path, change) -> None:
    value = json.loads(path.read_text())
    change(value)
    path.write_text(json.dumps(value, indent=2) + "\n")


def test_the_repository_history_is_intact_and_current():
    assert history.problems() == []
    lines = history.load_history()
    assert len(lines) >= 14
    assert json.loads(lines[-1])["snapshot"] == history.current()


def test_a_risk_class_change_fails_until_it_is_recorded_with_an_approver(copy):
    edit_json(copy / "contracts/tool_risks.json", lambda v: v["tools"].__setitem__("checked_add", "state_change"))
    assert history.problems(copy) == ["unrecorded permission change: tool_risk_classes.checked_add"]
    with pytest.raises(ValueError):
        history.record(copy, "widen checked_add", "")
    entry = history.record(copy, "widen checked_add for the incident drill", "repository owner")
    assert entry["changes"] == [{"path": "tool_risk_classes.checked_add", "before": "read_only",
                                 "after": "state_change"}]
    assert (entry["approved_by"], entry["source"]) == ("repository owner", "record")
    assert history.problems(copy) == []
    with pytest.raises(ValueError, match="no permission change"):
        history.record(copy, "again", "repository owner")


def test_workflow_token_and_launcher_approval_changes_are_permission_changes(copy):
    workflow = copy / ".github/workflows/python-verification.yml"
    workflow.write_text(workflow.read_text().replace("permissions:\n  contents: read\n",
                                                     "permissions:\n  contents: read\n  issues: write\n", 1))
    edit_json(copy / "config/local_tool.json", lambda v: v["sha256"].__setitem__("server_mcp.py", "0" * 64))
    assert history.problems(copy) == [
        "unrecorded permission change: launcher_approval.sha256.server_mcp.py, "
        "workflow_token_permissions.python-verification.yml.workflow.issues"]


def test_an_edited_history_is_detected(copy):
    path = copy / "docs/permission_history.jsonl"
    lines = path.read_text().splitlines()
    second = json.loads(lines[1])
    second["reason"] = "rewritten"
    lines[1] = json.dumps(second, sort_keys=True, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n")
    assert history.problems(copy) == ["entry 3: the chain was edited"]


def test_a_forged_snapshot_is_detected(copy):
    path = copy / "docs/permission_history.jsonl"
    lines = path.read_text().splitlines()
    last = json.loads(lines[-1])
    last["snapshot"]["tool_risk_classes"]["checked_add"] = "state_change"
    lines[-1] = json.dumps(last, sort_keys=True, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n")
    found = history.problems(copy)
    assert f"entry {len(lines)}: snapshot does not match its hash" in found
    assert any(problem.startswith("unrecorded permission change") for problem in found)


def test_past_permission_approvals_are_in_the_history():
    entries = [json.loads(line) for line in history.load_history()]
    by_commit = {entry["commit"][:7]: [c["path"] for c in entry["changes"]] for entry in entries if entry["commit"]}
    assert by_commit["98d45db"] == ["launcher_approval.sha256.contracts/tool_risks.json"]
    assert by_commit["49f6d66"] == ["tool_risk_classes.checked_multiply"]
    assert "external_destinations.github_issues.operation.approval" in by_commit["192e391"]
    assert any(path.endswith("sync-completed-issues.yml.jobs.sync.issues") for path in by_commit["28bfa57"])


def test_the_permission_reader_handles_workflow_and_job_scopes():
    text = ("permissions:\n  contents: read\njobs:\n  a:\n    permissions:\n      issues: write  # comment\n"
            "    steps: []\n  b:\n    permissions: read-all\n  c:\n    permissions: {}\n")
    assert history.workflow_permissions(text) == {"workflow": {"contents": "read"}, "jobs.a": {"issues": "write"},
                                                 "jobs.b": "read-all", "jobs.c": "{}"}


def test_ci_refuses_an_unrecorded_permission_change():
    workflow = (ROOT / ".github/workflows/python-verification.yml").read_text()
    assert "python scripts/permission_history.py check" in workflow
