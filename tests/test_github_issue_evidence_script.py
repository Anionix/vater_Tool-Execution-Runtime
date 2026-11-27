"""Dry run of scripts/run_github_issue_evidence.py against the in-memory fake.

This only proves the script's own logic and cleanup before a live CI run
creates real issues; it is not destination evidence.
"""
from importlib import util
import json
from pathlib import Path

from test_external_change import FakeGitHub
from verified_execution.external_change import HttpReply, TransportError

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


class LaggingRepository(FakeRepository):
    """The list endpoint hides recent issues for its first few reads, as a
    destination without read-after-write consistency may (seen live in run 1)."""

    def __init__(self, hidden_reads):
        super().__init__()
        self.hidden_reads = hidden_reads

    def request(self, method, path, *, json_body=None, query=None):
        if method == "GET" and path == "/issues" and self.hidden_reads > 0:
            self.hidden_reads -= 1
            return HttpReply(200, {}, [])
        return super().request(method, path, json_body=json_body, query=query)


def test_evidence_checks_wait_for_a_lagging_list_without_resending(tmp_path, monkeypatch):
    script = load_script()
    fake = LaggingRepository(hidden_reads=3)
    monkeypatch.setattr(script, "UrllibTransport", lambda repository, token: fake)
    monkeypatch.setattr(script.time, "sleep", lambda _seconds: None)
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/repository")
    monkeypatch.setenv("GITHUB_TOKEN", "dummy-token-for-the-fake")
    output = tmp_path / "report.json"
    monkeypatch.setattr("sys.argv", ["run_github_issue_evidence.py", "--output", str(output)])

    assert script.main() == 0
    report = json.loads(output.read_text())
    assert all(all(s["checks"].values()) for s in report["scenarios"])
    assert fake.posts == 2 and len(fake.issues) == 2  # waiting never turned into a resend


class SelectivelyLaggingRepository(FakeRepository):
    """Hides recent issues only on chosen list reads (1-based)."""

    def __init__(self, hidden):
        super().__init__()
        self.hidden, self.reads = set(hidden), 0

    def request(self, method, path, *, json_body=None, query=None):
        if method == "GET" and path == "/issues":
            self.reads += 1
            if self.reads in self.hidden:
                return HttpReply(200, {}, [])
        return super().request(method, path, json_body=json_body, query=query)


def test_issues_found_only_by_the_harness_are_still_closed(tmp_path, monkeypatch):
    # Reads 2 and 3 are the executor's two lookups in lost_reply; read 4 is the
    # harness's own check, which sees the issue (Codex review on #382).
    script = load_script()
    fake = SelectivelyLaggingRepository(hidden={2, 3})
    monkeypatch.setattr(script, "UrllibTransport", lambda repository, token: fake)
    monkeypatch.setattr(script.time, "sleep", lambda _seconds: None)
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/repository")
    monkeypatch.setenv("GITHUB_TOKEN", "dummy-token-for-the-fake")
    output = tmp_path / "report.json"
    monkeypatch.setattr("sys.argv", ["run_github_issue_evidence.py", "--output", str(output)])

    assert script.main() == 1  # the failed scenario stays a failure
    report = json.loads(output.read_text())
    assert {s["name"]: s["passed"] for s in report["scenarios"]}["lost_reply"] is False
    assert len(fake.issues) == 2 and sorted(fake.closed) == [1, 2]


class FlakyReadRepository(FakeRepository):
    """Fails only the second GET of /issues/1: the harness's own verification
    read right after the executor's snapshot (Codex review on #382)."""

    def __init__(self):
        super().__init__()
        self.reads = 0

    def request(self, method, path, *, json_body=None, query=None):
        if method == "GET" and path == "/issues/1":
            self.reads += 1
            if self.reads == 2:
                raise TransportError("ConnectionResetError")
        return super().request(method, path, json_body=json_body, query=query)


def test_a_scenario_that_raises_still_closes_its_issues_and_writes_the_report(tmp_path, monkeypatch):
    script = load_script()
    fake = FlakyReadRepository()
    monkeypatch.setattr(script, "UrllibTransport", lambda repository, token: fake)
    monkeypatch.setattr(script.time, "sleep", lambda _seconds: None)
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/repository")
    monkeypatch.setenv("GITHUB_TOKEN", "dummy-token-for-the-fake")
    output = tmp_path / "report.json"
    monkeypatch.setattr("sys.argv", ["run_github_issue_evidence.py", "--output", str(output)])

    assert script.main() == 1
    report = json.loads(output.read_text())
    assert report["all_passed"] is False and "TransportError" in report["error"]
    assert len(fake.issues) == 1 and fake.closed == [1] and report["closed"] == [1]
    assert all(issue["state"] == "closed" for issue in fake.issues)


def test_an_issue_no_reply_named_is_still_closed_through_its_marker(tmp_path, monkeypatch):
    # The lost-reply create answers 500 after creating, so no 201 reply names it, and
    # the executor's own lookups (list reads 2 and 3) miss it. Only the harness's
    # marker lookup finds it, and it must still be closed (regression REG-007).
    script = load_script()
    fake = SelectivelyLaggingRepository(hidden={2, 3})
    fake.script = ["ok", "server_error_after_create"]
    monkeypatch.setattr(script, "UrllibTransport", lambda repository, token: fake)
    monkeypatch.setattr(script.time, "sleep", lambda _seconds: None)
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/repository")
    monkeypatch.setenv("GITHUB_TOKEN", "dummy-token-for-the-fake")
    output = tmp_path / "report.json"
    monkeypatch.setattr("sys.argv", ["run_github_issue_evidence.py", "--output", str(output)])

    script.main()
    assert len(fake.issues) == 2 and sorted(fake.closed) == [1, 2]
    assert all(issue["state"] == "closed" for issue in fake.issues)
