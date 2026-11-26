"""Dry run of scripts/run_github_issue_evidence.py against the in-memory fake.

This only proves the script's own logic and cleanup before a live CI run
creates real issues; it is not destination evidence.
"""
from importlib import util
import json
from pathlib import Path

from test_external_change import FakeGitHub
from verified_execution.external_change import HttpReply

ROOT = Path(__file__).resolve().parents[1]


class FakeRepository(FakeGitHub):
    def __init__(self):
        super().__init__()
        self.labels, self.closed = set(), []

    def request(self, method, path, *, json_body=None, query=None):
        if path.startswith("/labels/") and method == "GET":
            return HttpReply(200 if path.split("/", 2)[2] in self.labels else 404, {}, {})
        if path == "/labels" and method == "POST":
            self.labels.add(json_body["name"])
            return HttpReply(201, {}, json_body)
        if method == "PATCH" and path.startswith("/issues/"):
            number = int(path.rsplit("/", 1)[1])
            self.issues[number - 1]["state"] = json_body["state"]
            self.closed.append(number)
            return HttpReply(200, {}, self.issues[number - 1])
        return super().request(method, path, json_body=json_body, query=query)


def load_script():
    spec = util.spec_from_file_location("github_issue_evidence", ROOT / "scripts/run_github_issue_evidence.py")
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_evidence_script_runs_all_scenarios_and_closes_what_it_created(tmp_path, monkeypatch):
    script = load_script()
    fake = FakeRepository()
    monkeypatch.setattr(script, "UrllibTransport", lambda repository, token: fake)
    monkeypatch.setattr(script.time, "sleep", lambda _seconds: None)
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/repository")
    monkeypatch.setenv("GITHUB_TOKEN", "dummy-token-for-the-fake")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    output = tmp_path / "report.json"
    monkeypatch.setattr("sys.argv", ["run_github_issue_evidence.py", "--output", str(output)])

    assert script.main() == 0
    report = json.loads(output.read_text())
    assert report["all_passed"] is True
    assert {s["name"]: s["passed"] for s in report["scenarios"]} == {
        "normal": True, "replay": True, "lost_reply": True, "crash": True, "target_check": True}
    # Two real creates (normal, lost_reply); the crash never reached the destination.
    assert len(fake.issues) == 2 and fake.posts == 2
    assert sorted(fake.closed) == sorted(report["closed"]) == sorted(report["issues"]) == [1, 2]
    assert "vater-external-evidence" in fake.labels
    assert "dummy-token" not in output.read_text()
