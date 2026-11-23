"""Import contract checks; no remote GitHub operations."""
import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "import_github_issues.py"
spec = importlib.util.spec_from_file_location("issue_importer", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_identifier_is_stable_across_title_edits():
    assert module.task_identifier("[task.trust.001] old") == "task.trust.001"
    assert module.task_identifier("[task.trust.001] new") == "task.trust.001"
    assert module.task_identifier("[gate.scope.completed] work") == "gate.scope.completed"
    assert module.task_identifier("no ID here") is None


def test_known_38_issues_do_not_get_recreated():
    tasks = module.load_candidates(Path(__file__).resolve().parents[1] / "backlog" / "github_issues.jsonl")
    known = {x["task_id"] for x in tasks if x["task_id"].startswith("gate.")}
    known |= {"task.scope.015"} | {f"task.trust.{n:03d}" for n in range(1, 19)}
    assert len(known) == 38
    existing = [f"[{task_id}] changed title" for task_id in known]
    missing = module.missing_candidates(tasks, existing)
    assert len(missing) == 313
    assert {x["task_id"] for x in missing}.isdisjoint(known)


def test_duplicate_ids_rejected(tmp_path):
    entry = {"task_id": "task.scope.001", "title": "[task.scope.001] name", "body": "..."}
    path = tmp_path / "issues.jsonl"
    path.write_text("\n".join(json.dumps(entry) for _ in range(351)), encoding="utf-8")
    try:
        module.load_candidates(path)
    except ValueError as exc:
        assert "351_UNIQUE_TASK_IDS_REQUIRED" in str(exc)
    else:
        raise AssertionError("duplicate IDs accepted")
