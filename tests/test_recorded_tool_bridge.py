"""OFFLINE bridge tests. They are NOT official MCP wire compatibility tests."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3

import pytest

from verified_execution.core import LocalAuthority
from verified_execution.ledger import OperationLedger, LedgerError
from verified_execution.recorded_tool_bridge import RecordedToolBridge


def bridge(tmp_path: Path, principal="trusted-process", permitted=True):
    db = OperationLedger(tmp_path / "operations.sqlite3")
    auth = LocalAuthority.calculator_only() if permitted else LocalAuthority(frozenset())
    return db, RecordedToolBridge(db, principal=principal, authority=auth)


def call(tool, key="req-1", left=5, right=8):
    return tool.call("checked_add_recorded", {"request_key": key, "left": left, "right": right})


def test_bridge_success(tmp_path):
    db, tool = bridge(tmp_path)
    outcome = call(tool)
    assert outcome.ok and outcome.value == 13 and not outcome.replayed
    assert db.history(outcome.operation_id) == ["reserved", "dispatched", "definitive_succeeded"]


def test_bridge_replay_after_new_service_instance(tmp_path):
    db, tool = bridge(tmp_path)
    first = call(tool)
    _, restarted = bridge(tmp_path)
    second = call(restarted)
    assert second.ok and second.value == first.value and second.replayed
    assert second.operation_id == first.operation_id
    assert db.history(first.operation_id).count("dispatched") == 1


def test_bridge_key_collision_rejected(tmp_path):
    db, tool = bridge(tmp_path)
    first = call(tool)
    second = call(tool, right=9)
    assert first.ok and not second.ok and second.error_code == "IDEMPOTENCY_CONFLICT"
    assert db.history(first.operation_id).count("dispatched") == 1


@pytest.mark.parametrize("arguments", [
    {"request_key": "ok", "left": 1},
    {"request_key": "ok", "left": 1, "right": 2, "admin": True},
    {"request_key": True, "left": 1, "right": 2},
    {"request_key": "", "left": 1, "right": 2},
    {"request_key": "x" * 257, "left": 1, "right": 2},
    {"request_key": "x\x00y", "left": 1, "right": 2},
    {"request_key": "\ud800", "left": 1, "right": 2},
    [1, 2],
    None,
])
def test_bridge_malformed_rejected_without_ledger_entry(tmp_path, arguments):
    db, tool = bridge(tmp_path)
    result = tool.call("checked_add_recorded", arguments)
    assert not result.ok
    with sqlite3.connect(db.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0


def test_bridge_unauthorized_cannot_dispatch(tmp_path):
    db, tool = bridge(tmp_path, permitted=False)
    result = call(tool)
    assert not result.ok and result.error_code == "NOT_AUTHORIZED"
    with sqlite3.connect(db.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0


def test_bridge_principal_isolated(tmp_path):
    _, a = bridge(tmp_path, principal="alice")
    _, b = bridge(tmp_path, principal="bob")
    first = call(a)
    second = call(b)
    assert first.ok and second.ok and first.operation_id != second.operation_id
    assert not second.replayed


def test_bridge_unknown_tool(tmp_path):
    _, tool = bridge(tmp_path)
    assert tool.call("bogus", {}).error_code == "UNKNOWN_TOOL"


def test_bridge_parallel_replay_uses_one_dispatch(tmp_path):
    db, tool = bridge(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: call(tool), range(25)))
    assert sum(not x.replayed for x in results) == 1
    assert all(x.ok and x.value == 13 for x in results if not x.replayed)
    # Pending duplicates may return IN_PROGRESS before the result is committed.
    assert all(x.ok or x.error_code == "IN_PROGRESS" for x in results)
    assert db.history(results[0].operation_id).count("dispatched") == 1


def test_atomic_completion_rejects_invalid_value(tmp_path):
    db, _ = bridge(tmp_path)
    op = db.reserve(principal="alice", request_key="explicit",tool_name="checked_add",
                    target_id=None, resource_version=None,arguments={"left": 1, "right": 2},
                    requires_approval=False)
    db.begin_dispatch(op.operation_id)
    with pytest.raises(LedgerError, match="INVALID_CALCULATION_RESULT"):
        db.complete_calculation(op.operation_id, -1)
    assert db.get(op.operation_id).state == "dispatched"
    assert db.read_calculation(op.operation_id, principal="alice") is None
    db.complete_calculation(op.operation_id, 3)
    assert db.read_calculation(op.operation_id, principal="alice") == 3
    assert db.read_calculation(op.operation_id, principal="bob") is None


def test_old_success_without_result_fails_closed(tmp_path):
    db, _ = bridge(tmp_path)
    op = db.reserve(principal="alice", request_key="old",tool_name="checked_add",
                    target_id=None, resource_version=None,arguments={"left": 1, "right": 2},
                    requires_approval=False)
    db.begin_dispatch(op.operation_id)
    db.record_definitive_result(op.operation_id, "succeeded")
    _, restarted = bridge(tmp_path, principal="alice")
    r = call(restarted, key="old",left=1,right=2)
    assert not r.ok and r.error_code == "ALREADY_RECORDED"


def test_unknown_never_autoretries(tmp_path):
    db, _ = bridge(tmp_path, principal="alice")
    op = db.reserve(principal="alice", request_key="unknown",tool_name="checked_add",
                    target_id=None, resource_version=None,arguments={"left": 1, "right": 2},
                    requires_approval=False)
    db.begin_dispatch(op.operation_id)
    db.mark_unknown(op.operation_id)
    _, service = bridge(tmp_path, principal="alice")
    outcome = call(service, key="unknown",left=1,right=2)
    assert not outcome.ok and outcome.error_code == "OUTCOME_UNKNOWN"
    assert db.history(op.operation_id).count("dispatched") == 1


def test_inflight_replay_is_explicit_and_not_redispatched(tmp_path, monkeypatch):
    """A deterministic pause separates DISPATCHED from definitive completion."""
    import threading
    import verified_execution.recorded_calculator as module
    entered = threading.Event()
    proceed = threading.Event()
    original = module.checked_add
    def slow_calculation(left, right):
        entered.set()
        assert proceed.wait(timeout=8)
        return original(left, right)
    monkeypatch.setattr(module, "checked_add", slow_calculation)
    db, tool = bridge(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(call, tool)
        assert entered.wait(timeout=5)
        repeated = call(tool)
        assert repeated.error_code == "IN_PROGRESS"
        assert repeated.replayed and not repeated.ok
        proceed.set()
        final = first.result(timeout=8)
    assert final.ok and final.value == 13
    from_cache = call(tool)
    assert from_cache.ok and from_cache.replayed and from_cache.value == 13
    assert db.history(final.operation_id).count("dispatched") == 1


@pytest.mark.parametrize("mode", ["recorded", "leased", "asynchronous"])
@pytest.mark.parametrize("loss", ["expired", "revoked"])
@pytest.mark.parametrize("when", ["before_request", "during_read"])
def test_replay_requires_live_host_grant(tmp_path, monkeypatch, mode, loss, when):
    from threading import Event
    import anyio
    from verified_execution import core
    from verified_execution.cancellable_bridge import CancellableLeasedToolBridge
    from verified_execution.worker_leases import LeasedLedger, LeasedToolBridge

    clock = [10]
    monkeypatch.setattr(core, "monotonic_ns", lambda: clock[0])
    revoked = Event()
    authority = LocalAuthority(frozenset({"checked_add"}), expires_at_monotonic_ns=11,
                               revocation_event=revoked)
    db_type = OperationLedger if mode == "recorded" else LeasedLedger
    db = db_type(tmp_path / "records.sqlite")
    def lose_grant():
        if loss == "expired":
            clock[0] = 11  # At the exact deadline, the grant has expired.
        else:
            revoked.set()

    async def run():
        bridge_type = {"recorded": RecordedToolBridge, "leased": LeasedToolBridge,
                       "asynchronous": CancellableLeasedToolBridge}[mode]
        bridge = bridge_type(db, principal="host-user", authority=authority)
        async def request():
            args = {"left": 2, "right": 3, "request_key": "stable"}
            if mode == "asynchronous":
                return await bridge.acall("checked_add_recorded", args)
            return bridge.call("checked_add_recorded", args)
        first = await request()
        assert first.ok and first.value == 5
        replay = await request()
        assert replay.ok and replay.replayed and replay.operation_id == first.operation_id
        history = db.history(first.operation_id)
        if when == "during_read":
            read = db.read_calculation
            def read_then_lose(*args, **kwargs):
                value = read(*args, **kwargs)
                lose_grant()
                return value
            monkeypatch.setattr(db, "read_calculation", read_then_lose)
        else:
            lose_grant()
        denied = await request()
        assert denied.error_code == "NOT_AUTHORIZED"
        assert not denied.ok and denied.value is None and denied.operation_id is None
        assert db.history(first.operation_id) == history
    anyio.run(run)
