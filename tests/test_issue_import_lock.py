"""Offline tests of single-writer GitHub Issue importer behavior.

IMPORT-TEST-001: branch lock is obtained before writes.
IMPORT-TEST-002: lock is released on both success and error.
IMPORT-TEST-003: lock conflicts fail closed; never steal another writer's lock.
IMPORT-TEST-004: closed duplicate Issues still count as registered.
"""
import importlib.util
from pathlib import Path
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "import_github_issues.py"
spec = importlib.util.spec_from_file_location("issue_import_lock_module", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

REPO = "Anionix/vater_Tool-Execution-Runtime"


def make_fake_calls(monkeypatch, *, fail_post=False):
    events = []

    def fake_gh(*args):
        events.append(tuple(args))
        if args[:2] == ("repo", "view"):
            return "main\n"
        if args[:1] == ("api",) and "-X" not in args:
            return "a" * 40 + "\n"
        if args[:3] == ("api", "-X", "POST") and fail_post:
            raise RuntimeError("already exists")
        return ""

    monkeypatch.setattr(module, "gh", fake_gh)
    return events


def test_lock_acquire_and_release(monkeypatch):
    calls = make_fake_calls(monkeypatch)
    with module.repository_import_lock(REPO):
        assert any(call[:3] == ("api", "-X", "POST") for call in calls)
        assert not any(call[:3] == ("api", "-X", "DELETE") for call in calls)
    assert calls[-1][:3] == ("api", "-X", "DELETE")


def test_lock_released_after_failure(monkeypatch):
    calls = make_fake_calls(monkeypatch)
    with pytest.raises(ValueError):
        with module.repository_import_lock(REPO):
            raise ValueError("failed to create issue")
    assert calls[-1][:3] == ("api", "-X", "DELETE")


def test_lock_conflict_does_not_delete_someone_elses_lock(monkeypatch):
    calls = make_fake_calls(monkeypatch, fail_post=True)
    with pytest.raises(RuntimeError, match="IMPORT_LOCK_UNAVAILABLE"):
        with module.repository_import_lock(REPO):
            pytest.fail("must not enter locked section")
    assert not any(call[:3] == ("api", "-X", "DELETE") for call in calls)


def test_remote_closed_issues_detected_by_stable_id(monkeypatch):
    calls = []
    def fake_gh(*args):
        calls.append(args)
        return '[{"title":"[task.scope.001] changed title"},' \
               '{"title":"[task.scope.002] closed duplicate"},' \
               '{"title":"unrelated issue"}]'
    monkeypatch.setattr(module, "gh", fake_gh)
    assert module.remote_issue_identifiers(REPO) == {"task.scope.001", "task.scope.002"}
    assert "--state" in calls[0] and "all" in calls[0]
