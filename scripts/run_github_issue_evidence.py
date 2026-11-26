#!/usr/bin/env python3
"""Live evidence for the durable_operations tasks against real GitHub Issues.

Run only by .github/workflows/external-change-evidence.yml, with the job's
GITHUB_TOKEN (issues: write) for GITHUB_REPOSITORY. Every issue it creates
carries the vater-external-evidence label and is closed before exit.

Scenarios (serial, at least one second between mutating requests):
  normal       create, then read back result and version      (011 012 021)
  replay       resend the same request_key: no second create  (015 017)
  lost_reply   real create, reply dropped: reconcile by lookup (011 016 017)
  crash        process dies before the request leaves         (011 016)
  target_check the stored object never matches another op     (021)

The JSON report records what was observed, including outcomes that went to
human reconciliation. Exit status 1 if any expectation failed.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from verified_execution.core import LocalAuthority  # noqa: E402
from verified_execution.external_change import (  # noqa: E402
    EVIDENCE_LABEL, TOOL_NAME, ExternalChangeExecutor, ExternalResultStore, GitHubIssueDestination,
    TransportError, UrllibTransport)
from verified_execution.ledger import OperationLedger  # noqa: E402


class Crash(BaseException):
    pass


class Paced:
    """Serial requests; mutating ones wait one second and issue creates are counted."""

    def __init__(self, inner):
        self.inner, self.posts, self.mode = inner, 0, "pass"

    def request(self, method, path, *, json_body=None, query=None):
        if method in ("POST", "PATCH"):
            time.sleep(1.0)
        if method == "POST" and path == "/issues":
            if self.mode == "crash_before_send":
                raise Crash()
            self.posts += 1
            reply = self.inner.request(method, path, json_body=json_body, query=query)
            if self.mode == "drop_reply":
                raise TransportError("reply dropped by the evidence harness after a real create")
            return reply
        return self.inner.request(method, path, json_body=json_body, query=query)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    repository, token = os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"]
    run = os.environ.get("GITHUB_RUN_ID", str(int(time.time())))
    transport = Paced(UrllibTransport(repository, token))
    destination = GitHubIssueDestination(transport)
    grant = LocalAuthority(frozenset({TOOL_NAME}))
    database = Path(tempfile.mkdtemp(prefix="vater-evidence-")) / "ledger.sqlite3"
    report = {"destination": "github_issues", "repository": repository, "run_id": run,
              "contract": "contracts/github_issue_destination.json", "scenarios": [], "issues": [], "closed": []}

    def host():
        ledger = OperationLedger(database)
        counter = {"n": 0}

        def approve(op, arguments):
            # The person who triggered this workflow run is the approver.
            counter["n"] += 1
            approval_id = f"run-{run}-{op.operation_id}-{counter['n']}"
            ledger.issue_approval(approval_id=approval_id, principal=op.principal, tool_name=op.tool_name,
                                  target_id=op.target_id, resource_version=op.resource_version,
                                  arguments=arguments, expires_at_ms=int(time.time() * 1000) + 300_000)
            return approval_id
        executor = ExternalChangeExecutor(ledger, ExternalResultStore(database), destination,
                                          principal="evidence-workflow", authority=grant,
                                          target_id=repository, approve=approve)
        return ledger, executor

    def marked(operation_id):
        try:
            return destination.lookup(operation_id, int(time.time() * 1000) - 600_000)
        except Exception as error:  # report, never hide
            return f"lookup failed: {error}"

    def record(name, tasks, expected, observed, passed):
        report["scenarios"].append({"name": name, "tasks": tasks, "expected": expected,
                                    "observed": observed, "passed": bool(passed)})

    def call(executor, key, title):
        return executor.call(TOOL_NAME, {"request_key": key, "title": title})

    ensure_label(transport)

    # normal + replay ---------------------------------------------------------
    ledger, executor = host()
    title = f"VATER evidence {run} normal"
    first = call(executor, f"{run}-normal", title)
    stored = executor.result(first.operation_id) if first.operation_id else None
    issue = transport.request("GET", f"/issues/{first.value}") if first.ok else None
    if first.ok:
        report["issues"].append(first.value)
    record("normal", ["task.durable_operations.011", "task.durable_operations.012", "task.durable_operations.021"],
           "created once; ledger reserved->dispatched->definitive_succeeded; stored result and version",
           {"ok": first.ok, "error": first.error_code, "issue": first.value,
            "history": ledger.history(first.operation_id) if first.operation_id else None,
            "stored": stored, "live_etag": issue.headers.get("etag") if issue else None},
           first.ok and stored and stored["status"] == "succeeded" and stored["version"]["etag"]
           and ledger.history(first.operation_id) == ["reserved", "dispatched", "definitive_succeeded"]
           and destination.check(first.operation_id, title, issue.body))
    posts_before = transport.posts
    again = call(executor, f"{run}-normal", title)
    matches = marked(first.operation_id) if first.operation_id else "no operation"
    record("replay", ["task.durable_operations.015", "task.durable_operations.017"],
           "same request_key: stored result returned, no new POST, exactly one issue with the marker",
           {"ok": again.ok, "replayed": again.replayed, "issue": again.value,
            "new_posts": transport.posts - posts_before,
            "issues_with_marker": len(matches) if isinstance(matches, list) else matches},
           again.ok and again.replayed and again.value == first.value and transport.posts == posts_before
           and isinstance(matches, list) and len(matches) == 1)

    # lost reply -----------------------------------------------------------------
    transport.mode = "drop_reply"
    lost_title = f"VATER evidence {run} lost reply"
    lost = call(executor, f"{run}-lost", lost_title)
    transport.mode = "pass"
    posts_after_lost = transport.posts
    stored_lost = executor.result(lost.operation_id) if lost.operation_id else None
    if not lost.ok:
        time.sleep(10)  # the list may lag; a replay looks up again and never resends
        lost = call(executor, f"{run}-lost", lost_title)
    matches = marked(lost.operation_id) if lost.operation_id else "no operation"
    if lost.ok:
        report["issues"].append(lost.value)
    record("lost_reply", ["task.durable_operations.011", "task.durable_operations.016",
                          "task.durable_operations.017"],
           "real create with the reply dropped: one POST only, reconciled by lookup (directly or after a "
           "replay), exactly one issue with the marker",
           {"ok": lost.ok, "error": lost.error_code, "issue": lost.value,
            "first_store_status": stored_lost and stored_lost["status"],
            "posts": posts_after_lost - posts_before, "posts_after_replay": transport.posts - posts_before,
            "issues_with_marker": len(matches) if isinstance(matches, list) else matches},
           lost.ok and transport.posts - posts_before == 1 and isinstance(matches, list) and len(matches) == 1)

    # crash before the request leaves ------------------------------------------------
    transport.mode = "crash_before_send"
    crashed_key = f"{run}-crash"
    try:
        call(executor, crashed_key, f"VATER evidence {run} crash")
        crashed_cleanly = False
    except Crash:
        crashed_cleanly = True
    transport.mode = "pass"
    ledger, executor = host()  # a restarted process
    recovered = ledger.recover_interrupted()
    posts_before_crash_replay = transport.posts
    after = call(executor, crashed_key, f"VATER evidence {run} crash")
    matches = marked(after.operation_id) if after.operation_id else "no operation"
    record("crash", ["task.durable_operations.011", "task.durable_operations.016"],
           "dispatched survives the crash and becomes unknown on restart; replay looks up, finds nothing, "
           "sends nothing and requires human reconciliation",
           {"crashed": crashed_cleanly, "recovered_operations": recovered, "error": after.error_code,
            "ledger_state": ledger.get(after.operation_id).state if after.operation_id else None,
            "new_posts": transport.posts - posts_before_crash_replay,
            "issues_with_marker": len(matches) if isinstance(matches, list) else matches},
           crashed_cleanly and recovered == 1 and after.error_code == "HUMAN_RECONCILIATION_REQUIRED"
           and transport.posts == posts_before_crash_replay and matches == [])

    # target check on a real object ---------------------------------------------------
    if first.ok and issue is not None:
        own = destination.check(first.operation_id, title, issue.body)
        foreign = destination.check(after.operation_id or "other-operation", title, issue.body)
        renamed = destination.check(first.operation_id, "another title", issue.body)
        record("target_check", ["task.durable_operations.021"],
               "the real issue matches its own operation only; another operation or title is refused",
               {"own": own, "other_operation": foreign, "other_title": renamed}, own and not foreign and not renamed)

    # cleanup ------------------------------------------------------------------------
    for number in report["issues"]:
        reply = transport.request("PATCH", f"/issues/{number}",
                                  json_body={"state": "closed", "state_reason": "not_planned"})
        if reply.status == 200:
            report["closed"].append(number)
    report["all_passed"] = (len(report["scenarios"]) == 5 and all(s["passed"] for s in report["scenarios"])
                            and sorted(report["closed"]) == sorted(report["issues"]))
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"all_passed": report["all_passed"],
                      "scenarios": {s["name"]: s["passed"] for s in report["scenarios"]}}))
    return 0 if report["all_passed"] else 1


def ensure_label(transport) -> None:
    if transport.request("GET", f"/labels/{EVIDENCE_LABEL}").status == 404:
        transport.request("POST", "/labels", json_body={
            "name": EVIDENCE_LABEL, "color": "bfd4f2",
            "description": "Created and closed by the VATER external-change evidence workflow"})


if __name__ == "__main__":
    raise SystemExit(main())
