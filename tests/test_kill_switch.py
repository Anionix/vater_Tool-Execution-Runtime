"""task.operations.015: an external tool can be stopped immediately.

The kill switch is a file; engaging it revokes the grant at every check the
executor and the ledger already make, so no request leaves the process after
it is seen: not before reservation, not between approval and dispatch, and
not as a resend after a rate-limit wait. It stays engaged until restart,
fails closed when its path cannot be checked, and stops the live evidence
workflow before its first request.
"""
from __future__ import annotations

from importlib import util
import json
from pathlib import Path

from test_external_change import REPO, FakeGitHub, Host
from test_github_issue_evidence_script import FakeRepository, load_script
from verified_execution.core import LocalAuthority
from verified_execution.external_change import TOOL_NAME
from verified_execution.kill_switch import ENV_VAR, KillSwitch, default_path
from verified_execution.ledger import OperationLedger

ROOT = Path(__file__).resolve().parents[1]
spec = util.spec_from_file_location("kill_switch_cli", ROOT / "scripts" / "kill_switch.py")
cli = util.module_from_spec(spec)
spec.loader.exec_module(cli)


def guarded_host(tmp_path, github, **kwargs):
    switch = KillSwitch(tmp_path / "kill-switch")
    host = Host(tmp_path / "ledger.sqlite3", github, **kwargs)
    host.executor.authority = LocalAuthority(frozenset({TOOL_NAME}), revocation_event=switch)
    return host, switch


def test_an_engaged_switch_refuses_before_anything_is_reserved_or_sent(tmp_path):
    github = FakeGitHub()
    host, switch = guarded_host(tmp_path, github)
    assert host.create("k-0").ok and github.posts == 1  # the tool works while the switch is off
    switch.engage("incident")
    result = host.create("k-1")
    assert (result.ok, result.error_code, result.operation_id) == (False, "NOT_AUTHORIZED", None)
    assert github.posts == 1 and len(github.issues) == 1


def test_engaged_between_approval_and_dispatch_the_ledger_refuses_the_dispatch(tmp_path):
    github = FakeGitHub()
    host, switch = guarded_host(tmp_path, github)
    approve = host.approve

    def approve_then_incident(op, arguments):
        approval_id = approve(op, arguments)
        switch.engage("incident during approval")
        return approval_id

    host.executor.approve = approve_then_incident
    result = host.create("k-1")
    assert (result.ok, result.error_code) == (False, "NOT_AUTHORIZED")
    assert github.posts == 0
    assert OperationLedger(host.path).get(result.operation_id).state == "prepared"  # never dispatched


def test_engaged_during_a_rate_limit_wait_nothing_is_resent(tmp_path):
    github = FakeGitHub()
    github.script = ["rate_limited", "ok"]
    host, switch = guarded_host(tmp_path, github)
    host.executor.sleep = lambda seconds: switch.engage("incident during backoff")
    result = host.create("k-1")
    assert (result.ok, result.error_code) == (False, "NOT_AUTHORIZED")
    assert github.posts == 1 and github.issues == []  # the refused first attempt only


def test_the_switch_is_sticky_until_restart_and_fails_closed(tmp_path, monkeypatch):
    switch = KillSwitch(tmp_path / "kill-switch")
    assert switch.is_set() is False
    (tmp_path / "kill-switch").write_text("stop\n")
    assert switch.is_set() is True
    (tmp_path / "kill-switch").unlink()
    assert switch.is_set() is True  # removing the file does not re-enable a running process
    assert KillSwitch(tmp_path / "kill-switch").is_set() is False  # a restarted process
    unreadable = KillSwitch(tmp_path / "unreadable")

    def refuse(self, *args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "exists", refuse)
    assert unreadable.is_set() is True


def test_without_the_variable_the_switch_lives_in_the_home_directory(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert default_path() == tmp_path / ".vater" / "kill-switch"
    assert KillSwitch.from_env().path == tmp_path / ".vater" / "kill-switch"


def test_an_engage_that_cannot_write_its_file_raises_but_still_stops_this_process(tmp_path):
    (tmp_path / "not-a-directory").write_text("")
    switch = KillSwitch(tmp_path / "not-a-directory" / "kill-switch")
    try:
        switch.engage("incident")
    except OSError:
        pass
    else:
        raise AssertionError("the operator must see that the file was not written")
    assert switch.is_set() is True


def test_the_operator_command_engages_reports_and_clears(tmp_path, monkeypatch, capsys):
    path = tmp_path / "ops" / "kill-switch"
    monkeypatch.setenv(ENV_VAR, str(path))
    assert cli.main(["status"]) == 0 and json.loads(capsys.readouterr().out)["engaged"] is False
    assert cli.main(["engage", "--reason", "incident 42"]) == 0
    assert json.loads(capsys.readouterr().out) == {"path": str(path), "engaged": True}
    assert "incident 42" in path.read_text()
    assert KillSwitch.from_env().is_set() is True
    assert cli.main(["clear"]) == 0 and json.loads(capsys.readouterr().out)["engaged"] is False


def test_the_live_evidence_run_stops_before_its_first_request(tmp_path, monkeypatch):
    script = load_script()
    fake = FakeRepository()
    requests = []
    original = fake.request

    def recording(method, path, **kwargs):
        requests.append((method, path))
        return original(method, path, **kwargs)

    fake.request = recording
    monkeypatch.setattr(script, "UrllibTransport", lambda repository, token: fake)
    monkeypatch.setattr(script.time, "sleep", lambda _seconds: None)
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setenv("GITHUB_TOKEN", "dummy-token-for-the-fake")
    switch = tmp_path / "kill-switch"
    switch.write_text("stop\n")
    monkeypatch.setenv(ENV_VAR, str(switch))
    output = tmp_path / "report.json"
    monkeypatch.setattr("sys.argv", ["run_github_issue_evidence.py", "--output", str(output)])
    assert script.main() == 1
    report = json.loads(output.read_text())
    assert report["stopped_by_kill_switch"] == str(switch) and report["all_passed"] is False
    assert requests == [] and fake.posts == 0


def test_the_workflow_switch_is_wired_through_the_environment_and_the_runbook_names_the_steps():
    workflow = (ROOT / ".github/workflows/external-change-evidence.yml").read_text()
    engage = workflow.index("python -I scripts/kill_switch.py engage")
    run = workflow.index("python -I scripts/run_github_issue_evidence.py")
    assert engage < run
    assert workflow.count("VATER_KILL_SWITCH_FILE: ${{ runner.temp }}/vater-kill-switch") == 2
    assert "DISABLED: ${{ vars.VATER_EXTERNAL_TOOLS_DISABLED }}" in workflow
    assert "${{ vars." not in workflow.split("run: |", 1)[1].split("- name:", 1)[0]  # no inline expansion
    runbook = (ROOT / "docs/runbooks/disable_external_tools.md").read_text()
    for step in ("python scripts/kill_switch.py engage", "VATER_EXTERNAL_TOOLS_DISABLED",
                 "restart", "HUMAN_RECONCILIATION_REQUIRED", "python scripts/kill_switch.py clear"):
        assert step in runbook, step
