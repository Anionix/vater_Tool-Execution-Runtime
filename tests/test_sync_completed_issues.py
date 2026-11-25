"""Exercise the completion workflow against a simulated GitHub repository."""
import importlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "Anionix/vater_Tool-Execution-Runtime"


@pytest.fixture
def remote(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    sync = importlib.import_module("sync_completed_issues")
    rows = sync.read_json_lines((ROOT / sync.INDEX).read_text(encoding="utf-8"))
    issues = {
        row["issue_number"]: {
            "number": row["issue_number"],
            "title": f'[{row["task_id"]}] Existing title',
            "state": "closed" if row["task_status"] == "done" else "open",
        }
        for row in rows
    }
    completed = next(row["issue_number"] for row in rows if row["task_status"] == "done")
    unfinished = next(row["issue_number"] for row in rows if row["task_status"] != "done")
    issues[completed]["state"] = "open"
    updates = []

    def github(*args):
        if args[1] == "--paginate":
            return "\n".join(map(json.dumps, issues.values()))
        if args[1] == "--method":
            number = int(args[3].rsplit("/", 1)[-1])
            updates.append(number)
            assert args[4:] == ("-f", "state=closed", "-f", "state_reason=completed")
            issues[number].update(state="closed", state_reason="completed")
            return json.dumps(issues[number])
        if args[-1] == ".default_branch":
            return "main"
        assert args[-1] == ".object.sha"
        return "current-head"

    real_git = sync.git

    def git(*args):
        if args[0] == "status":
            return ""
        if args[0] == "rev-parse":
            return "current-head"
        return real_git(*args)

    monkeypatch.setattr(sync, "gh", github)
    monkeypatch.setattr(sync, "git", git)
    return sync, issues, updates, completed, unfinished


def test_preview_apply_and_retry(remote):
    sync, issues, updates, completed, unfinished = remote
    sync.synchronize(REPOSITORY, apply=False)
    assert not updates
    sync.synchronize(REPOSITORY, apply=True)
    sync.synchronize(REPOSITORY, apply=True)
    assert updates == [completed]
    assert issues[unfinished]["state"] == "open"


def test_mismatched_identifier_blocks_entire_batch(remote):
    sync, issues, updates, completed, unfinished = remote
    issues[completed]["title"] = issues[unfinished]["title"]
    with pytest.raises(ValueError, match="does not match"):
        sync.synchronize(REPOSITORY, apply=True)
    assert not updates


def test_stale_index_blocks_completion(remote):
    sync, *_ = remote
    tasks = sync.read_json_lines((ROOT / sync.LEDGER).read_text(encoding="utf-8"))
    index = sync.read_json_lines((ROOT / sync.INDEX).read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="index is stale"):
        sync.completed_numbers(tasks, index, "stale-blob", REPOSITORY)


def test_old_checkout_blocks_completion(remote, monkeypatch):
    sync, _, updates, *_ = remote
    git = sync.git
    monkeypatch.setattr(sync, "git", lambda *args: "old-head" if args[0] == "rev-parse" else git(*args))
    with pytest.raises(ValueError, match="current default branch"):
        sync.synchronize(REPOSITORY, apply=True)
    assert not updates
