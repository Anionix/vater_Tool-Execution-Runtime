"""External-change executor against an in-memory GitHub fake.

These are unit tests of this repository's side of the protocol. They are
NOT destination evidence: the durable_operations tasks close only on a live
run against GitHub (scripts/run_github_issue_evidence.py in CI).
"""
import asyncio
import time

import pytest

from verified_execution.core import LocalAuthority
from verified_execution.external_change import (
    EVIDENCE_LABEL, TOOL_NAME, ExternalChangeExecutor, ExternalResultStore, GitHubIssueDestination,
    HttpReply, TransportError, github_test_create_issue_spec)
from verified_execution.ledger import OperationLedger
from verified_execution.runtime import ToolRuntime
from verified_execution.tools import default_registry

GRANT = LocalAuthority(frozenset({TOOL_NAME}))
REPO = "example/repository"


class Crash(BaseException):
    """A process dying mid-request (not an ordinary exception)."""


class FakeGitHub:
    def __init__(self):
        self.issues, self.posts, self.script = [], 0, []
        self.list_lag = self.lookup_fails = False
        self.on_post = None

    def _create(self, body):
        issue = {"number": len(self.issues) + 1, "node_id": f"I_{len(self.issues) + 1}", "title": body["title"],
                 "body": body["body"], "labels": [{"name": name} for name in body["labels"]],
                 "updated_at": "2026-11-26T00:00:00Z"}
        self.issues.append(issue)
        return issue

    def request(self, method, path, *, json_body=None, query=None):
        if method == "POST" and path == "/issues":
            self.posts += 1
            if self.on_post is not None:
                self.on_post(json_body)
            step = self.script.pop(0) if self.script else "ok"
            if step == "crash_before_create":
                raise Crash()
            if step == "rate_limited":
                return HttpReply(403, {"retry-after": "2"}, {"message": "secondary rate limit"})
            if step == "server_error_no_create":
                return HttpReply(502, {}, None)
            if step == "reject":
                return HttpReply(422, {}, {"message": "Validation Failed"})
            issue = self._create(json_body)
            if step == "crash_after_create":
                raise Crash()
            if step == "drop_reply":
                raise TransportError("TimeoutError")
            if step == "server_error_after_create":
                return HttpReply(500, {}, None)
            if step == "wrong_title":
                return HttpReply(201, {}, {**issue, "title": "someone else's issue"})
            return HttpReply(201, {}, issue)
        if method == "GET" and path == "/issues":
            if self.lookup_fails:
                raise TransportError("ConnectionResetError")
            visible = [] if self.list_lag else [
                issue for issue in self.issues if query["labels"] in {l["name"] for l in issue["labels"]}]
            page = query["page"]
            return HttpReply(200, {}, visible[(page - 1) * 100: page * 100])
        if method == "GET" and path.startswith("/issues/"):
            issue = self.issues[int(path.rsplit("/", 1)[1]) - 1]
            return HttpReply(200, {"etag": f'W/"etag-{issue["number"]}"'}, issue)
        raise AssertionError(f"unexpected request {method} {path}")


class Host:
    """Wires the executor the way a host would; approvals are host-issued."""

    def __init__(self, path, github, *, approve=True, max_attempts=2):
        self.path, self.github, self.slept, self.approvals = path, github, [], 0
        self.ledger = OperationLedger(path)
        self.store = ExternalResultStore(path)
        self.executor = ExternalChangeExecutor(
            self.ledger, self.store, GitHubIssueDestination(github), principal="local-host",
            authority=GRANT, target_id=REPO, approve=self.approve if approve else None,
            max_attempts=max_attempts, sleep=self.slept.append)

    def approve(self, op, arguments):
        self.approvals += 1
        approval_id = f"approval-{self.approvals}"
        self.ledger.issue_approval(approval_id=approval_id, principal=op.principal, tool_name=op.tool_name,
                                   target_id=op.target_id, resource_version=op.resource_version,
                                   arguments=arguments, expires_at_ms=int(time.time() * 1000) + 60_000)
        return approval_id

    def create(self, key="k-1", title="Evidence issue"):
        return self.executor.call(TOOL_NAME, {"request_key": key, "title": title})

    def state(self, operation_id):
        return OperationLedger(self.path).get(operation_id).state


@pytest.fixture
def github():
    return FakeGitHub()


@pytest.fixture
def host(tmp_path, github):
    return Host(tmp_path / "ledger.sqlite3", github)


def marked_operation(body):
    return body["body"].rsplit("vater-operation-id: ", 1)[1]


# --- approval -----------------------------------------------------------------

def test_no_request_leaves_without_a_host_approval(tmp_path, github):
    host = Host(tmp_path / "l.sqlite3", github, approve=False)
    result = host.create()
    assert (result.ok, result.error_code) == (False, "APPROVAL_REQUIRED")
    assert github.posts == 0 and host.state(result.operation_id) == "prepared"


# --- task.durable_operations.011 ---------------------------------------------

def test_dispatched_state_is_durable_before_the_request(host, github):
    seen = []
    github.on_post = lambda body: seen.append((OperationLedger(host.path).get(marked_operation(body)).state,
                                               ExternalResultStore(host.path).get(marked_operation(body))["status"]))
    assert host.create().ok
    assert seen == [("dispatched", "dispatched")]


def test_crash_after_creation_is_recovered_by_lookup_not_resent(host, github):
    github.script = ["crash_after_create"]
    with pytest.raises(Crash):
        host.create()
    restarted = Host(host.path, github)
    assert restarted.ledger.recover_interrupted() == 1
    result = restarted.create()
    assert (result.ok, result.value, result.replayed) == (True, 1, True)
    assert github.posts == 1 and len(github.issues) == 1
    assert restarted.store.get(result.operation_id)["status"] == "reconciled"
    assert restarted.state(result.operation_id) == "unknown"  # the ledger claim is never rewritten


def test_crash_before_the_destination_received_goes_to_a_person(host, github):
    github.script = ["crash_before_create"]
    with pytest.raises(Crash):
        host.create()
    restarted = Host(host.path, github)
    restarted.ledger.recover_interrupted()
    result = restarted.create()
    assert result.error_code == "HUMAN_RECONCILIATION_REQUIRED"
    assert github.posts == 1 and github.issues == []


# --- task.durable_operations.012 / .021 ---------------------------------------

def test_result_and_version_are_stored_by_operation_id(host, github):
    result = host.create(title="Stored result")
    stored = host.executor.result(result.operation_id)
    assert (stored["status"], stored["object_id"], stored["destination"]) == ("succeeded", "1", "github_issues")
    assert stored["version"]["etag"] == 'W/"etag-1"'
    assert stored["version"]["updated_at"] == "2026-11-26T00:00:00Z" and len(stored["version"]["content_sha256"]) == 64
    assert host.state(result.operation_id) == "succeeded"
    assert host.ledger.history(result.operation_id) == ["reserved", "dispatched", "definitive_succeeded"]


def test_object_that_does_not_match_the_request_is_refused(host, github):
    github.script = ["wrong_title"]
    result = host.create()
    assert result.error_code == "TARGET_MISMATCH"
    assert host.state(result.operation_id) == "unknown"
    assert host.store.get(result.operation_id)["status"] == "human_required"


# --- task.durable_operations.015 / .017: no idempotency, no blind resend -----

@pytest.mark.parametrize("step", ["drop_reply", "server_error_after_create"])
def test_possibly_executed_create_is_reconciled_never_resent(host, github, step):
    github.script = [step]
    result = host.create()
    assert (result.ok, result.value) == (True, 1)
    assert host.store.get(result.operation_id)["status"] == "reconciled"
    again = host.create()
    assert (again.ok, again.value, again.replayed) == (True, 1, True)
    assert github.posts == 1 and len(github.issues) == 1


def test_resend_happens_only_after_a_proven_non_execution(host, github):
    github.script = ["rate_limited", "ok"]
    result = host.create()
    assert result.ok and github.posts == 2 and host.slept == [2.0] and len(github.issues) == 1


def test_resends_are_bounded(host, github):
    github.script = ["rate_limited", "rate_limited", "ok"]
    result = host.create()
    assert result.error_code == "RATE_LIMITED" and github.posts == 2
    assert host.state(result.operation_id) == "failed"
    assert host.create().error_code == "RATE_LIMITED"  # replay reports, never resends
    assert github.posts == 2


def test_rejection_is_final(host, github):
    github.script = ["reject"]
    assert host.create().error_code == "REJECTED_BY_DESTINATION" and github.posts == 1


# --- task.durable_operations.016: unresolved outcomes go to a person ---------

def test_absence_in_a_lagging_list_is_not_proof(host, github):
    github.script, github.list_lag = ["drop_reply"], True
    result = host.create()
    assert result.error_code == "HUMAN_RECONCILIATION_REQUIRED"
    assert host.store.get(result.operation_id)["detail"] == "NO_MATCH"
    github.list_lag = False  # once the destination shows the issue, a replay reconciles
    again = host.create()
    assert (again.ok, again.value, again.replayed) == (True, 1, True)
    assert github.posts == 1


def test_failed_lookup_goes_to_a_person(host, github):
    github.script, github.lookup_fails = ["server_error_no_create"], True
    result = host.create()
    assert result.error_code == "HUMAN_RECONCILIATION_REQUIRED"
    assert host.store.get(result.operation_id)["detail"] == "LOOKUP_INCOMPLETE"


def test_duplicate_matches_go_to_a_person(host, github):
    github.script = ["drop_reply"]
    github.on_post = lambda body: github._create(body)  # someone else created the same marker
    result = host.create()
    assert result.error_code == "HUMAN_RECONCILIATION_REQUIRED"
    assert host.store.get(result.operation_id)["detail"] == "MANY_MATCHES"


# --- the tool on the shared runtime -------------------------------------------

def test_external_tool_runs_through_the_shared_runtime(host, github):
    registry = default_registry().with_tools(github_test_create_issue_spec())
    runtime = ToolRuntime(GRANT, registry, durable={TOOL_NAME: host.executor})
    outcome = asyncio.run(runtime.acall(TOOL_NAME, {"request_key": "rt", "title": "Via runtime"}))
    assert outcome.ok and outcome.structured() == {"result": 1, "operation_id": outcome.operation_id,
                                                   "replayed": False}
    for bad in ({"request_key": "rt", "title": " padded "}, {"request_key": "rt", "title": "x" * 121},
                {"request_key": "rt", "title": "line\nbreak"}, {"request_key": "", "title": "t"},
                {"request_key": "rt", "title": "t", "labels": ["admin"]}):
        assert not asyncio.run(runtime.acall(TOOL_NAME, bad)).ok
    assert github.posts == 1
    denied = ToolRuntime(LocalAuthority(frozenset()), registry, durable={TOOL_NAME: host.executor})
    assert asyncio.run(denied.acall(TOOL_NAME, {"request_key": "no", "title": "t"})).error_code == "NOT_AUTHORIZED"
    assert github.posts == 1


def test_issue_payload_is_marked_and_labelled():
    payload = GitHubIssueDestination(FakeGitHub()).payload("op-1", "T")
    assert payload["labels"] == [EVIDENCE_LABEL]
    assert payload["body"].splitlines()[-1] == "vater-operation-id: op-1"


# --- crash points around the success transition (Codex review on #382) -------

def test_crash_between_result_store_and_ledger_success_keeps_the_result(host, github, monkeypatch):
    def die(*_args, **_kwargs):
        raise Crash()
    monkeypatch.setattr(host.ledger, "record_definitive_result", die)
    with pytest.raises(Crash):
        host.create()
    restarted = Host(host.path, github)
    assert restarted.ledger.recover_interrupted() == 1
    result = restarted.create()
    assert (result.ok, result.value, result.replayed) == (True, 1, True)
    assert github.posts == 1


def test_succeeded_ledger_without_a_stored_result_is_recovered_by_lookup(host, github, monkeypatch):
    original = host.store.record

    def lose_success(operation_id, status, **kwargs):
        if status == "succeeded":
            return None  # simulate a result write that never landed
        return original(operation_id, status, **kwargs)
    monkeypatch.setattr(host.store, "record", lose_success)
    first = host.create()
    assert first.ok and host.state(first.operation_id) == "succeeded"
    assert host.store.get(first.operation_id)["status"] == "dispatched"
    restarted = Host(host.path, github)
    again = restarted.create()
    assert (again.ok, again.value, again.replayed) == (True, 1, True)
    assert restarted.store.get(first.operation_id)["status"] == "reconciled"
    assert github.posts == 1
