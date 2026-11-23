"""Deterministic request-by-request cancellation, using actual SQLite and AnyIO.

These are APPLICATION/HANDLER tests, NOT the MCP wire. The official Python MCP
SDK is responsible for cancellation notification matching and response
suppression. No fake network-level success is asserted here.
"""
from __future__ import annotations

import anyio
import pytest
from pathlib import Path

from verified_execution.cancellable_bridge import CancellableLeasedToolBridge
from verified_execution.core import LocalAuthority
from verified_execution.worker_leases import LeasedLedger
from verified_execution.ledger import LedgerError


def setup(tmp_path, **hooks):
    ledger=LeasedLedger(tmp_path/'operations.sqlite3')
    bridge=CancellableLeasedToolBridge(ledger, principal='local-trusted',
                                     authority=LocalAuthority.calculator_only(), **hooks)
    return ledger,bridge


def args(key='request-001', left=1, right=2):
    return {'request_key':key, 'left':left, 'right':right}


def operation(db, key):
    with db._open() as conn:
        row=conn.execute('SELECT operation_id FROM operations WHERE principal=? AND request_key=?',
                        ('local-trusted',key)).fetchone()
    assert row is not None, key
    return db.get(row[0])


def execute_async(fun):
    return anyio.run(fun)


def test_cancel_before_dispatch_persists_cancelled(tmp_path):
    entered=anyio.Event()
    async def scenario():
        gate=anyio.Event()
        async def hold(_op):
            entered.set()
            await gate.wait()
        db, bridge=setup(tmp_path, before_dispatch=hold)
        caught=[]
        async def invoke(scope):
            with scope:
                try: await bridge.acall('checked_add_recorded', args('before'))
                finally: caught.append(True)
        scope=anyio.CancelScope()
        async with anyio.create_task_group() as group:
            group.start_soon(invoke,scope)
            await entered.wait()
            scope.cancel()
        op=operation(db,'before')
        assert op.state=='cancelled' and not op.cancel_requested
        assert 'leased_dispatch' not in db.history(op.operation_id)
        assert caught
    execute_async(scenario)


def test_cancel_after_dispatch_becomes_unknown_never_cancelled(tmp_path):
    entered=anyio.Event()
    async def scenario():
        async def hold(_op):
            entered.set()
            await anyio.Event().wait()  # canceled by the SDK-like task scope
        db, bridge=setup(tmp_path, after_dispatch=hold)
        scope=anyio.CancelScope()
        async def invoke():
            with scope:
                await bridge.acall('checked_add_recorded', args('after'))
        async with anyio.create_task_group() as group:
            group.start_soon(invoke)
            await entered.wait()
            scope.cancel()
        op=operation(db,'after')
        assert op.state == 'unknown' and op.cancel_requested
        assert db.history(op.operation_id).count('leased_dispatch')==1
        assert any(event.endswith('outcome_unknown') for event in db.history(op.operation_id))
        assert db.read_calculation(op.operation_id,principal='local-trusted') is None
        retry=await bridge.acall('checked_add_recorded',args('after'))
        assert retry.replayed and not retry.ok and retry.error_code=='OUTCOME_UNKNOWN'
    execute_async(scenario)


def test_cancel_after_commit_preserves_success(tmp_path):
    entered=anyio.Event()
    async def scenario():
        async def hold(_op):
            entered.set()
            await anyio.Event().wait()
        db,bridge=setup(tmp_path,after_commit=hold)
        scope=anyio.CancelScope()
        async def invoke():
            with scope:
                await bridge.acall('checked_add_recorded',args('committed',20,22))
        async with anyio.create_task_group() as group:
            group.start_soon(invoke)
            await entered.wait()
            scope.cancel()
        op=operation(db,'committed')
        assert op.state=='succeeded' and not op.cancel_requested
        assert db.read_calculation(op.operation_id,principal='local-trusted') == 42
        assert db.history(op.operation_id).count('leased_dispatch')==1
        replay=await bridge.acall('checked_add_recorded',args('committed',20,22))
        assert replay.ok and replay.replayed and replay.value==42
    execute_async(scenario)


def test_cancellation_of_second_request_does_not_cancel_first(tmp_path):
    entered=anyio.Event()
    async def scenario():
        async def hold(_op):
            entered.set()
            await anyio.Event().wait()
        db,bridge=setup(tmp_path,after_dispatch=hold)
        scope=anyio.CancelScope()
        async def primary():
            with scope:
                await bridge.acall('checked_add_recorded',args('same'))
        async with anyio.create_task_group() as group:
            group.start_soon(primary)
            await entered.wait()
            replay=await bridge.acall('checked_add_recorded',args('same'))
            assert replay.replayed and replay.error_code=='IN_PROGRESS'
            assert operation(db,'same').cancel_requested is False
            scope.cancel()
        assert operation(db,'same').state=='unknown'
    execute_async(scenario)


def test_cancellation_of_one_of_two_distinct_requests_is_isolated(tmp_path):
    one_started=anyio.Event()
    async def scenario():
        async def hold(op):
            if op.request_key=='cancel-me':
                one_started.set()
                await anyio.Event().wait()
        db,bridge=setup(tmp_path,after_dispatch=hold)
        scope=anyio.CancelScope()
        async def first():
            with scope: await bridge.acall('checked_add_recorded',args('cancel-me'))
        async with anyio.create_task_group() as group:
            group.start_soon(first)
            await one_started.wait()
            other=await bridge.acall('checked_add_recorded',args('keep-me',5,6))
            assert other.ok and other.value==11
            scope.cancel()
        assert operation(db,'cancel-me').state=='unknown'
        assert operation(db,'keep-me').state=='succeeded'
        assert operation(db,'keep-me').cancel_requested is False
    execute_async(scenario)


@pytest.mark.parametrize('invalid',[
    {'request_key':'x','left':True,'right':2},
    {'request_key':'x','left':1.0,'right':2},
    {'request_key':'x','left':1},
    {'request_key':'x','left':1,'right':2,'principal':'admin'},
])
def test_invalid_request_creates_no_ledger_record(tmp_path,invalid):
    async def scenario():
        db,bridge=setup(tmp_path)
        response=await bridge.acall('checked_add_recorded',invalid)
        assert not response.ok
        with db._open() as conn:
            assert conn.execute('SELECT COUNT(*) FROM operations').fetchone()[0]==0
    execute_async(scenario)


def test_unauthorized_async_request_does_not_create_operations(tmp_path):
    async def scenario():
        db=LeasedLedger(tmp_path/'operations.sqlite3')
        bridge=CancellableLeasedToolBridge(db,principal='local-trusted',
                                            authority=LocalAuthority(frozenset()))
        result=await bridge.acall('checked_add_recorded',args())
        assert not result.ok and result.error_code=='NOT_AUTHORIZED'
        with db._open() as conn:
            assert conn.execute('SELECT COUNT(*) FROM operations').fetchone()[0]==0
    execute_async(scenario)


def test_two_sequential_requests_replay_from_committed_data(tmp_path):
    async def scenario():
        db,bridge=setup(tmp_path)
        first=await bridge.acall('checked_add_recorded',args('idempotent',123,400))
        second=await bridge.acall('checked_add_recorded',args('idempotent',123,400))
        assert first.ok and second.ok and first.value==second.value==523
        assert second.replayed and second.operation_id==first.operation_id
        assert db.history(first.operation_id).count('leased_dispatch')==1
    execute_async(scenario)


def test_cancelled_before_commit_does_not_generate_saved_value(tmp_path):
    reached_dispatch = anyio.Event()
    async def scenario():
        async def hold(_op):
            reached_dispatch.set()
            await anyio.Event().wait()
        db,bridge=setup(tmp_path,after_dispatch=hold)
        scope=anyio.CancelScope()
        async def invoke():
            with scope: await bridge.acall('checked_add_recorded',args('no-value'))
        async with anyio.create_task_group() as group:
            group.start_soon(invoke)
            with anyio.fail_after(3):
                await reached_dispatch.wait()
            scope.cancel()
        op=operation(db,'no-value')
        assert op.state=='unknown'
        assert db.read_calculation(op.operation_id,principal='local-trusted') is None
    execute_async(scenario)


def test_different_owner_cannot_cancel_other_workers_dispatched_operation(tmp_path):
    db=LeasedLedger(tmp_path/'forged-owner.sqlite3')
    owner=db.new_session(duration_ms=20000)
    outsider=db.new_session(duration_ms=20000)
    op=db.reserve(principal='local-trusted',request_key='owned',tool_name='checked_add',
                  target_id=None,resource_version=None,
                  arguments={'left':1,'right':2},requires_approval=False)
    db.start(op.operation_id,token=owner.token)
    with pytest.raises(LedgerError) as failure:
        db.abandon_owner_on_cancel(op.operation_id,token=outsider.token)
    assert failure.value.code=='STALE_OR_DIFFERENT_OWNER'
    assert db.get(op.operation_id).state=='dispatched'
    assert not db.get(op.operation_id).cancel_requested


def test_expired_owner_cannot_reclassify_operation_as_cancelled(tmp_path):
    import time
    db=LeasedLedger(tmp_path/'stale-owner.sqlite3')
    owner=db.new_session(duration_ms=60)
    op=db.reserve(principal='local-trusted',request_key='stale',tool_name='checked_add',
                  target_id=None,resource_version=None,
                  arguments={'left':1,'right':2},requires_approval=False)
    db.start(op.operation_id,token=owner.token,duration_ms=60)
    time.sleep(.10)
    with pytest.raises(LedgerError) as failure:
        db.abandon_owner_on_cancel(op.operation_id,token=owner.token)
    assert failure.value.code=='STALE_OR_DIFFERENT_OWNER'
    # Dedicated recovery determines outcome; stale owner must not invent it.
    assert db.get(op.operation_id).state=='dispatched'
    assert db.recover_expired()==1
    assert db.get(op.operation_id).state=='unknown'
