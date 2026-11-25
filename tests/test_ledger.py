"""Independent deterministic tests for the persistent operation ledger.

Tests intentionally do not simulate a real remote service, network authentication,
or the MCP protocol cancellation wire contract.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import time

import pytest

from verified_execution.ledger import LedgerError, OperationLedger, fingerprint

BASE_ARGUMENTS = {"left": 1, "right": 2}


@pytest.fixture
def ledger(tmp_path: Path):
    return OperationLedger(tmp_path / "operations.sqlite3")


def reserve(ledger: OperationLedger, request_key="request-1", *,
            principal="alice", arguments=None, requires_approval=False,
            target_id="resource-1", resource_version="revision-1"):
    return ledger.reserve(
        principal=principal, request_key=request_key, tool_name="write_value",
        target_id=target_id, resource_version=resource_version,
        arguments=BASE_ARGUMENTS if arguments is None else arguments,
        requires_approval=requires_approval)


def approve(ledger: OperationLedger, op, approval_id="approval-1", *,
            tool_name="write_value", target_id="resource-1", resource_version="revision-1",
            arguments=None, expiry_add_ms=60000):
    ledger.issue_approval(
        approval_id=approval_id, principal=op.principal, tool_name=tool_name,
        target_id=target_id, resource_version=resource_version,
        arguments=BASE_ARGUMENTS if arguments is None else arguments,
        expires_at_ms=int(time.time() * 1000) + expiry_add_ms)


def fails(code, fn, *args, **kwargs):
    with pytest.raises(LedgerError) as error:
        fn(*args, **kwargs)
    assert error.value.code == code


def test_reservation_and_history(ledger):
    op = reserve(ledger)
    assert op.state == "prepared"
    assert op.operation_id
    assert ledger.history(op.operation_id) == ["reserved"]
    assert ledger.get(op.operation_id) == op


def test_same_request_same_fingerprint_returns_same_id(ledger):
    first = reserve(ledger, arguments={"left": 1, "right": 2})
    second = reserve(ledger, arguments={"right": 2, "left": 1})
    assert first.operation_id == second.operation_id
    assert second.replayed
    assert ledger.history(first.operation_id) == ["reserved"]


@pytest.mark.parametrize("change", [
    {"arguments": {"left": 2, "right": 2}},
    {"target_id": "resource-2"},
    {"resource_version": "revision-2"},
    {"requires_approval": True},
])
def test_request_reuse_with_changed_intent_rejected(ledger, change):
    reserve(ledger)
    fails("IDEMPOTENCY_CONFLICT", reserve, ledger, **change)


def test_principal_partition(ledger):
    first = reserve(ledger, principal="alice")
    second = reserve(ledger, principal="bob")
    assert first.operation_id != second.operation_id


def test_no_approval_for_write_is_rejected(ledger):
    op = reserve(ledger, requires_approval=True)
    fails("INVALID_APPROVAL_ID", ledger.begin_dispatch, op.operation_id)
    assert ledger.get(op.operation_id).state == "prepared"


def test_bound_approval_is_consumed_and_dispatches(ledger):
    op = reserve(ledger, requires_approval=True)
    approve(ledger, op)
    begun = ledger.begin_dispatch(op.operation_id, approval_id="approval-1")
    assert begun.state == "dispatched"
    fails("NOT_PREPARED", ledger.begin_dispatch, op.operation_id, approval_id="approval-1")
    assert ledger.history(op.operation_id) == ["reserved", "dispatched"]


@pytest.mark.parametrize("change", [
    {"tool_name": "other_tool"},
    {"target_id": "different"},
    {"resource_version": "other-version"},
    {"arguments": {"left": 2, "right": 1}},
])
def test_mismatched_approval_rejected(ledger, change):
    op = reserve(ledger, requires_approval=True)
    approve(ledger, op, **change)
    fails("APPROVAL_INVALID", ledger.begin_dispatch, op.operation_id,
          approval_id="approval-1")
    assert ledger.get(op.operation_id).state == "prepared"


def test_expired_approval_rejected(ledger):
    op = reserve(ledger, requires_approval=True)
    approve(ledger, op, expiry_add_ms=30)
    time.sleep(0.05)
    fails("APPROVAL_INVALID", ledger.begin_dispatch, op.operation_id,
          approval_id="approval-1")


def test_approval_cannot_be_reused_across_operations(ledger):
    one = reserve(ledger, "key-1", requires_approval=True)
    two = reserve(ledger, "key-2", requires_approval=True)
    approve(ledger, one)
    ledger.begin_dispatch(one.operation_id, approval_id="approval-1")
    fails("APPROVAL_INVALID", ledger.begin_dispatch, two.operation_id,
          approval_id="approval-1")


def test_cannot_accidentally_attach_approval_to_no_approval_operation(ledger):
    op = reserve(ledger)
    fails("APPROVAL_UNEXPECTED", ledger.begin_dispatch,
          op.operation_id, approval_id="anything")


def test_cancel_before_dispatch_is_terminal(ledger):
    op = reserve(ledger)
    cancelled = ledger.request_cancel(op.operation_id)
    assert cancelled.state == "cancelled"
    assert not cancelled.cancel_requested
    fails("NOT_PREPARED", ledger.begin_dispatch, op.operation_id)
    assert ledger.request_cancel(op.operation_id).state == "cancelled"


def test_cancel_after_dispatch_does_not_mean_undone(ledger):
    op = reserve(ledger)
    ledger.begin_dispatch(op.operation_id)
    cancellation = ledger.request_cancel(op.operation_id)
    assert cancellation.state == "dispatched"
    assert cancellation.cancel_requested
    assert ledger.request_cancel(op.operation_id) == cancellation
    assert ledger.record_definitive_result(op.operation_id, "succeeded").state == "succeeded"


def test_definitive_failure_must_be_dispatched(ledger):
    op = reserve(ledger)
    fails("NOT_DISPATCHED", ledger.record_definitive_result,
          op.operation_id, "failed")
    ledger.begin_dispatch(op.operation_id)
    assert ledger.record_definitive_result(op.operation_id, "failed").state == "failed"
    fails("NOT_DISPATCHED", ledger.record_definitive_result,
          op.operation_id, "succeeded")


def test_ambiguous_connection_fails_closed(ledger):
    op = reserve(ledger)
    ledger.begin_dispatch(op.operation_id)
    assert ledger.mark_unknown(op.operation_id).state == "unknown"
    fails("NOT_PREPARED", ledger.begin_dispatch, op.operation_id)
    assert ledger.request_cancel(op.operation_id).state == "unknown"
    assert ledger.reconcile(op.operation_id, result="succeeded",
                            evidence_reference="trusted-response-123").state == "succeeded"
    fails("NOT_UNKNOWN", ledger.reconcile, op.operation_id,
          result="failed", evidence_reference="new-response")


def test_restart_interrupted_marks_unknown(tmp_path):
    filename = tmp_path / "ledger.sqlite3"
    ledger_one = OperationLedger(filename)
    in_flight = reserve(ledger_one, "flight")
    idle = reserve(ledger_one, "idle")
    ledger_one.begin_dispatch(in_flight.operation_id)
    ledger_two = OperationLedger(filename)
    assert ledger_two.recover_interrupted() == 1
    assert ledger_two.recover_interrupted() == 0
    assert ledger_two.get(in_flight.operation_id).state == "unknown"
    assert ledger_two.get(idle.operation_id).state == "prepared"
    fails("NOT_PREPARED", ledger_two.begin_dispatch, in_flight.operation_id)


def test_events_persist_across_reopen(tmp_path):
    filename = tmp_path / "ledger.sqlite3"
    first = OperationLedger(filename)
    op = reserve(first)
    first.begin_dispatch(op.operation_id)
    first.mark_unknown(op.operation_id)
    reopened = OperationLedger(filename)
    assert reopened.history(op.operation_id) == ["reserved", "dispatched", "outcome_unknown"]


def test_approval_and_dispatch_roll_back_as_one_transaction(ledger):
    op = reserve(ledger, requires_approval=True)
    approve(ledger, op)
    fails("INVALID_APPROVAL_ID", ledger.begin_dispatch, op.operation_id,
          approval_id="")
    assert ledger.begin_dispatch(op.operation_id, approval_id="approval-1").state == "dispatched"


def test_parallel_reservations_return_one_id(ledger):
    with ThreadPoolExecutor(max_workers=8) as pool:
        operations = list(pool.map(lambda _: reserve(ledger), range(24)))
    assert len({op.operation_id for op in operations}) == 1
    assert sum(not op.replayed for op in operations) == 1


def test_parallel_dispatch_allows_one_winner(ledger):
    op = reserve(ledger, requires_approval=True)
    approve(ledger, op)
    def worker(_):
        try:
            ledger.begin_dispatch(op.operation_id, approval_id="approval-1")
            return "began"
        except LedgerError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(worker, range(24)))
    assert results.count("began") == 1
    assert results.count("NOT_PREPARED") == 23


def test_parallel_approval_consumption_only_one_winner(ledger):
    one = reserve(ledger, "key-1", requires_approval=True)
    two = reserve(ledger, "key-2", requires_approval=True)
    approve(ledger, one)
    def worker(op):
        try:
            ledger.begin_dispatch(op.operation_id, approval_id="approval-1")
            return "began"
        except LedgerError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, [one, two]))
    assert sorted(results) == ["APPROVAL_INVALID", "began"]
    assert sum(ledger.get(op.operation_id).state == "dispatched" for op in [one, two]) == 1


@pytest.mark.parametrize("bad", [1.0, {"left": float("nan")}, {1: "value"},
                                   set([1, 2])])
def test_invalid_fingerprint_data_rejected(bad):
    fails("INVALID_ARGUMENTS", fingerprint, bad)


def test_fingerprint_separates_boolean_and_integer():
    assert fingerprint({"number": True}) != fingerprint({"number": 1})


def test_fingerprint_rejects_too_much_data():
    fails("ARGUMENTS_TOO_LARGE", fingerprint, {"data": "x" * 17000})


def test_fingerprint_rejects_recursive_data():
    recursive = []
    recursive.append(recursive)
    fails("INVALID_ARGUMENTS", fingerprint, recursive)


def test_bad_identifier_and_flag_rejected(ledger):
    fails("INVALID_PRINCIPAL", reserve, ledger, principal="")
    fails("INVALID_REQUEST_KEY", reserve, ledger, request_key="\x00")
    fails("INVALID_APPROVAL_FLAG", reserve, ledger, requires_approval=1)


def test_duplicate_approval_identifier_rejected(ledger):
    op = reserve(ledger, requires_approval=True)
    approve(ledger, op)
    fails("DUPLICATE_APPROVAL", approve, ledger, op)


def test_ledger_cannot_be_memory_only():
    fails("FILE_DATABASE_REQUIRED", OperationLedger, ":memory:")


def test_sqlite_integrity_check_after_concurrent_writes(ledger):
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda index: reserve(ledger, f"key-{index}"), range(40)))
    with sqlite3.connect(ledger.path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 40
        assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 40


def test_reread_does_not_auto_retry_unknown(ledger):
    op = reserve(ledger)
    ledger.begin_dispatch(op.operation_id)
    ledger.mark_unknown(op.operation_id)
    replay = reserve(ledger)
    assert replay.state == "unknown"
    assert replay.replayed
    fails("NOT_PREPARED", ledger.begin_dispatch, replay.operation_id)


def test_real_crash_after_dispatch_recovers_as_unknown(tmp_path):
    """Crash is the actual death of a different process, not a simulated exception."""
    import subprocess
    import sys
    import os
    from pathlib import Path
    filename = tmp_path / "crash-ledger.sqlite3"
    code = """
import os, sys
from verified_execution.ledger import OperationLedger
ledger = OperationLedger(sys.argv[1])
op = ledger.reserve(principal='alice', request_key='crash',tool_name='write',
                    target_id='r1', resource_version='v1',arguments={'value':3},
                    requires_approval=False)
ledger.begin_dispatch(op.operation_id)
os._exit(13)
"""
    environ = dict(os.environ)
    environ["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    result = subprocess.run([sys.executable, "-c", code, str(filename)],
                            capture_output=True, env=environ, timeout=5)
    assert result.returncode == 13
    ledger = OperationLedger(filename)
    assert ledger.recover_interrupted() == 1
    with sqlite3.connect(filename) as connection:
        assert connection.execute(
            "SELECT state FROM operations WHERE request_key='crash'").fetchone() == ("unknown",)


def test_racing_cancel_and_result_never_claims_undo(tmp_path):
    for iteration in range(8):
        ledger = OperationLedger(tmp_path / f"race-{iteration}.sqlite3")
        op = reserve(ledger)
        ledger.begin_dispatch(op.operation_id)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(ledger.request_cancel, op.operation_id),
                pool.submit(ledger.record_definitive_result, op.operation_id, "succeeded")
            ]
            outcomes = [future.result() for future in futures]
        assert ledger.get(op.operation_id).state == "succeeded"
        assert all(outcome.state != "cancelled" for outcome in outcomes)


def test_parameterized_sql_does_not_execute_injected_text(ledger):
    attack = "resource'); DROP TABLE operations; --"
    op = reserve(ledger, request_key=attack, target_id=attack)
    assert ledger.get(op.operation_id).target_id == attack
    with sqlite3.connect(ledger.path) as connection:
        assert connection.execute("SELECT count(*) FROM operations").fetchone() == (1,)


def test_bad_timeout_rejected(tmp_path):
    fails("INVALID_TIMEOUT", OperationLedger, tmp_path / "bad.db", timeout_seconds=0)


@pytest.mark.parametrize("action", ["delete", "send", "purchase"])
def test_high_impact_reservation_defaults_to_approval(ledger, action):
    op = ledger.reserve(principal="local-user", request_key="one", tool_name=action,
                        target_id="example", resource_version="1", arguments={})
    assert op.requires_approval
    fails("INVALID_APPROVAL_ID", ledger.begin_dispatch, op.operation_id)
    assert ledger.get(op.operation_id).state == "prepared"


def test_approval_uses_time_after_acquiring_dispatch_lock(ledger, monkeypatch):
    from contextlib import contextmanager
    op = reserve(ledger, requires_approval=True)
    current = [10]
    monkeypatch.setattr(ledger, "_now", lambda: current[0])
    ledger.issue_approval(approval_id="approval-1", principal=op.principal,
                          tool_name=op.tool_name, target_id=op.target_id,
                          resource_version=op.resource_version, arguments=BASE_ARGUMENTS, expires_at_ms=20)
    original = ledger._transaction
    @contextmanager
    def acquired_later():
        with original() as connection:
            current[0] = 20
            yield connection
    monkeypatch.setattr(ledger, "_transaction", acquired_later)
    fails("APPROVAL_INVALID", ledger.begin_dispatch, op.operation_id, approval_id="approval-1")
    assert ledger.get(op.operation_id).state == "prepared"
