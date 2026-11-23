"""Deterministic cancellation tests: actual async tasks + persistent SQLite.

NO MCP SDK, NO JSON-RPC wire transport, NO external side effects are tested.
"""
from __future__ import annotations

import anyio
from pathlib import Path
import pytest

from verified_execution.cancellable_bridge import CancellableLeasedToolBridge
from verified_execution.core import LocalAuthority
from verified_execution.ledger import LedgerError
from verified_execution.worker_leases import LeasedLedger


ARGUMENTS = {"request_key": "cancel-test", "left": 20, "right": 22}


def create(tmp_path: Path, **hooks):
    ledger = LeasedLedger(tmp_path / "operations.sqlite3")
    return ledger, CancellableLeasedToolBridge(
        ledger, principal="trusted-host", authority=LocalAuthority.calculator_only(),
        **hooks)


def cancel_at_stage(tmp_path: Path, stage: str):
    arrived = anyio.Event()
    release = anyio.Event()
    operation_ids: list[str] = []
    cancelled = []

    async def checkpoint(operation):
        operation_ids.append(operation.operation_id)
        arrived.set()
        await release.wait()

    ledger, bridge = create(tmp_path, **{stage: checkpoint})

    async def scenario():
        async def perform():
            with anyio.CancelScope() as scope:
                scopes.append(scope)
                await bridge.acall("checked_add_recorded", ARGUMENTS)
                cancelled.append("unexpected-response")
        scopes = []
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(perform)
            with anyio.fail_after(3):
                await arrived.wait()
            assert len(scopes) == 1
            scopes[0].cancel()
        return operation_ids

    ids = anyio.run(scenario)
    assert len(ids) == 1 and not cancelled
    return ledger, bridge, ids[0]


def test_cancel_before_dispatch_is_terminal_and_never_dispatched(tmp_path):
    ledger, bridge, operation_id = cancel_at_stage(tmp_path, "before_dispatch")
    op = ledger.get(operation_id)
    assert op.state == "cancelled" and not op.cancel_requested
    assert ledger.history(operation_id) == ["reserved", "cancelled_before_dispatch"]
    assert ledger.read_calculation(operation_id, principal="trusted-host") is None
    replay = anyio.run(bridge.acall, "checked_add_recorded", ARGUMENTS)
    assert not replay.ok and replay.replayed and replay.error_code == "ALREADY_RECORDED"


def test_cancel_after_dispatch_is_unknown_and_no_redispatch(tmp_path):
    ledger, bridge, operation_id = cancel_at_stage(tmp_path, "after_dispatch")
    op = ledger.get(operation_id)
    assert op.state == "unknown" and op.cancel_requested
    events = ledger.history(operation_id)
    assert events[:2] == ["reserved", "leased_dispatch"]
    assert len(events) == 3 and events[-1].endswith("_outcome_unknown")
    assert ledger.read_calculation(operation_id, principal="trusted-host") is None
    replay = anyio.run(bridge.acall, "checked_add_recorded", ARGUMENTS)
    assert not replay.ok and replay.replayed and replay.error_code == "OUTCOME_UNKNOWN"
    assert ledger.history(operation_id).count("leased_dispatch") == 1


def test_cancel_after_successful_commit_does_not_erase_result(tmp_path):
    ledger, bridge, operation_id = cancel_at_stage(tmp_path, "after_commit")
    assert ledger.get(operation_id).state == "succeeded"
    assert ledger.read_calculation(operation_id, principal="trusted-host") == 42
    assert ledger.history(operation_id) == ["reserved", "leased_dispatch", "leased_completed"]
    replay = anyio.run(bridge.acall, "checked_add_recorded", ARGUMENTS)
    assert replay.ok and replay.replayed and replay.value == 42


def test_normal_async_path_returns_correct_result(tmp_path):
    ledger, bridge = create(tmp_path)
    result = anyio.run(bridge.acall, "checked_add_recorded", ARGUMENTS)
    assert result.ok and result.value == 42 and not result.replayed
    assert ledger.get(result.operation_id).state == "succeeded"


def test_normal_async_overflow_is_definitive_failure(tmp_path):
    ledger, bridge = create(tmp_path)
    args = {**ARGUMENTS, "left": 4294967295, "right": 1}
    response = anyio.run(bridge.acall, "checked_add_recorded", args)
    assert response.error_code == "OVERFLOW" and not response.ok
    assert ledger.get(response.operation_id).state == "failed"


@pytest.mark.parametrize("bad,expected", [
    ({"request_key": True, "left": 1, "right": 2}, "INVALID_REQUEST_KEY"),
    ({"request_key": "", "left": 1, "right": 2}, "INVALID_REQUEST_KEY"),
    ({"request_key": "ok", "left": 1.0, "right": 2}, "INVALID_ARGUMENT"),
    ({"request_key": "ok", "left": True, "right": 2}, "INVALID_ARGUMENT"),
    ({"request_key": "ok", "left": 1, "right": 2, "admin": True}, "INVALID_ARGUMENT"),
    ({"request_key": "ok", "left": 1}, "INVALID_ARGUMENT"),
    ({"request_key": "x"*257, "left": 1, "right": 2}, "INVALID_REQUEST_KEY"),
])
def test_invalid_arguments_no_ledger_entry(tmp_path, bad, expected):
    ledger, bridge = create(tmp_path)
    result = anyio.run(bridge.acall, "checked_add_recorded", bad)
    assert result.error_code == expected and not result.ok
    with ledger._open() as connection:
        assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 0


def test_cancel_then_separate_request_can_succeed(tmp_path):
    """Independent request keys have independent cancellation scopes."""
    arrived = anyio.Event()
    blocker = anyio.Event()
    async def block_first(op):
        if op.request_key == "cancel-first":
            arrived.set()
            await blocker.wait()
    ledger, bridge = create(tmp_path, before_dispatch=block_first)
    async def scenario():
        active = []
        async def first():
            with anyio.CancelScope() as scope:
                active.append(scope)
                await bridge.acall("checked_add_recorded", {
                    **ARGUMENTS, "request_key": "cancel-first"})
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(first)
            with anyio.fail_after(3):
                await arrived.wait()
            second = await bridge.acall("checked_add_recorded", {
                **ARGUMENTS, "request_key": "normal-second"})
            active[0].cancel()
        assert second.ok and second.value == 42
    anyio.run(scenario)
    with ledger._open() as connection:
        states = dict(connection.execute("SELECT request_key,state FROM operations"))
    assert states == {"cancel-first":"cancelled", "normal-second":"succeeded"}


def test_lease_fencing_prevents_other_worker_cancel(tmp_path):
    ledger = LeasedLedger(tmp_path / "lease.sqlite3")
    a, b = ledger.new_session(), ledger.new_session()
    op = ledger.reserve(principal="p", request_key="key", tool_name="checked_add",
                        target_id=None, resource_version=None,
                        arguments={"left":1,"right":2},requires_approval=False)
    ledger.start(op.operation_id, token=a.token)
    with pytest.raises(LedgerError, match="STALE_OR_DIFFERENT_OWNER"):
        ledger.abandon_owner_on_cancel(op.operation_id, token=b.token)
    assert ledger.get(op.operation_id).state == "dispatched"
    changed = ledger.abandon_owner_on_cancel(op.operation_id, token=a.token)
    assert changed.state == "unknown" and changed.cancel_requested
    with pytest.raises(LedgerError, match="NOT_DISPATCHED"):
        ledger.abandon_owner_on_cancel(op.operation_id, token=a.token)


def test_expired_owner_cannot_write_cancellation_state(tmp_path):
    ledger = LeasedLedger(tmp_path / "lease.sqlite3")
    session = ledger.new_session(duration_ms=50)
    op = ledger.reserve(principal="p",request_key="short", tool_name="checked_add",
                        target_id=None,resource_version=None,
                        arguments={"left":1,"right":2},requires_approval=False)
    ledger.start(op.operation_id, token=session.token, duration_ms=50)
    anyio.run(anyio.sleep, .085)
    with pytest.raises(LedgerError, match="STALE_OR_DIFFERENT_OWNER"):
        ledger.abandon_owner_on_cancel(op.operation_id, token=session.token)
    assert ledger.recover_expired() == 1
    assert ledger.get(op.operation_id).state == "unknown"
