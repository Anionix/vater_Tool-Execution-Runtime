"""Regression tests for request cancellation while SQLite/threads are waiting.

Machine contract:
  SQLITE-WAIT-001: other event loop tasks can progress during a DB wait.
  SQLITE-WAIT-002: no abandoned SQL task can commit after cancellation cleanup.
  SQLITE-WAIT-003: dispatch completion is never mislabeled as pre-dispatch.
  SQLITE-WAIT-004: a busy DB has a bounded, machine-readable failure.
These are actual SQLite operations, not official MCP wire tests.
"""
from __future__ import annotations

import anyio
import sqlite3
import threading
import time
from pathlib import Path

from verified_execution.cancellable_bridge import CancellableLeasedToolBridge
from verified_execution.core import LocalAuthority
from verified_execution.worker_leases import LeasedLedger


def bridge_for(ledger):
    return CancellableLeasedToolBridge(ledger, principal='host',
                                      authority=LocalAuthority.calculator_only(),
                                      duration_ms=20000)


def arguments(key):
    return {'request_key': key, 'left': 10, 'right': 20}


async def eventually(signal: threading.Event):
    with anyio.fail_after(2):
        while not signal.is_set():
            await anyio.sleep(.003)


def rows(ledger, request_key):
    with ledger._open() as conn:
        return conn.execute(
            "SELECT operation_id,state,cancel_requested FROM operations WHERE request_key=?",
            (request_key,)).fetchall()


def test_cancel_while_reservation_worker_is_blocked(tmp_path):
    ledger = LeasedLedger(tmp_path / 'reserve.sqlite3', timeout_seconds=.4)
    original = ledger.reserve
    entered = threading.Event()
    release = threading.Event()

    def delayed_reserve(**kwargs):
        entered.set()
        assert release.wait(timeout=3), 'test synchronization timed out'
        return original(**kwargs)

    ledger.reserve = delayed_reserve
    events = []

    async def scenario():
        scope = anyio.CancelScope()
        async def request():
            with scope:
                await bridge_for(ledger).acall('checked_add_recorded', arguments('reserve'))
                events.append('response-leaked')
        async with anyio.create_task_group() as group:
            group.start_soon(request)
            await eventually(entered)
            # SQLite wait occurs in a worker; the event loop can still run.
            await anyio.sleep(.025)
            events.append('event-loop-responsive')
            scope.cancel()
            release.set()
    anyio.run(scenario)
    assert events == ['event-loop-responsive']
    assert len(rows(ledger,'reserve')) == 1
    assert rows(ledger,'reserve')[0][1] == 'cancelled'


def test_cancel_during_dispatch_worker_does_not_misclassify(tmp_path):
    ledger = LeasedLedger(tmp_path / 'dispatch.sqlite3',timeout_seconds=.4)
    original = ledger.start
    entered = threading.Event()
    release = threading.Event()

    def delayed_start(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=3)
        return original(*args, **kwargs)

    ledger.start = delayed_start

    async def scenario():
        scope = anyio.CancelScope()
        async def request():
            with scope:
                await bridge_for(ledger).acall('checked_add_recorded',arguments('dispatch'))
                raise AssertionError('cancelled request unexpectedly returned')
        async with anyio.create_task_group() as group:
            group.start_soon(request)
            await eventually(entered)
            scope.cancel()
            release.set()
    anyio.run(scenario)
    info=rows(ledger,'dispatch')
    assert len(info)==1
    assert info[0][1:] == ('unknown',1), info
    assert ledger.history(info[0][0]).count('leased_dispatch') == 1
    assert ledger.read_calculation(info[0][0],principal='host') is None


def test_real_sqlite_write_lock_does_not_block_event_loop(tmp_path):
    ledger=LeasedLedger(tmp_path / 'lock.sqlite3',timeout_seconds=.4)
    held=threading.Event()
    release=threading.Event()

    def lock_database():
        with sqlite3.connect(ledger.path, isolation_level=None, timeout=.4) as conn:
            conn.execute('BEGIN IMMEDIATE')
            held.set()
            assert release.wait(3)
            conn.execute('COMMIT')

    worker=threading.Thread(target=lock_database)
    worker.start()
    assert held.wait(2)
    async def scenario():
        scope=anyio.CancelScope()
        finished=[]
        async def request():
            with scope:
                await bridge_for(ledger).acall('checked_add_recorded',arguments('locked'))
                finished.append('response')
        async with anyio.create_task_group() as group:
            group.start_soon(request)
            # If sqlite ran on event loop, sleep would stall behind the DB writer.
            with anyio.fail_after(.22):
                await anyio.sleep(.035)
            scope.cancel()
            release.set()
        assert not finished
    try:
        anyio.run(scenario)
    finally:
        release.set()
        worker.join(timeout=2)
    assert not worker.is_alive()
    assert rows(ledger,'locked') == []


def test_locked_db_is_reported_without_fake_success(tmp_path):
    ledger=LeasedLedger(tmp_path / 'busy.sqlite3',timeout_seconds=.09)
    held=threading.Event(); release=threading.Event()
    def lock_database():
        with sqlite3.connect(ledger.path, isolation_level=None, timeout=.5) as conn:
            conn.execute('BEGIN IMMEDIATE'); held.set(); assert release.wait(3); conn.execute('COMMIT')
    worker=threading.Thread(target=lock_database)
    worker.start()
    assert held.wait(2)
    try:
        async def scenario():
            return await bridge_for(ledger).acall('checked_add_recorded',arguments('busy'))
        result=anyio.run(scenario)
        assert not result.ok and result.error_code == 'DATABASE_BUSY', result
    finally:
        release.set();worker.join(timeout=2)
    assert rows(ledger,'busy') == []


def test_cancel_during_result_commit_preserves_definitive_success(tmp_path):
    ledger = LeasedLedger(tmp_path / 'finish.sqlite3',timeout_seconds=.4)
    original = ledger.finish
    entered = threading.Event()
    release = threading.Event()

    def delayed_finish(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=3)
        return original(*args, **kwargs)

    ledger.finish = delayed_finish
    responses = []

    async def scenario():
        scope = anyio.CancelScope()
        async def request():
            with scope:
                result = await bridge_for(ledger).acall('checked_add_recorded',arguments('finish'))
                responses.append(result)
        async with anyio.create_task_group() as group:
            group.start_soon(request)
            await eventually(entered)
            scope.cancel()
            release.set()
    anyio.run(scenario)
    info = rows(ledger,'finish')
    assert len(info) == 1 and info[0][1:] == ('succeeded',0)
    assert ledger.read_calculation(info[0][0],principal='host') == 30
    assert not responses  # SDK still owns network-level response suppression
    assert ledger.history(info[0][0]).count('leased_dispatch') == 1


def test_cancel_during_session_creation_does_not_start_operation(tmp_path):
    ledger = LeasedLedger(tmp_path / 'session.sqlite3',timeout_seconds=.4)
    original = ledger.new_session
    entered = threading.Event()
    release = threading.Event()
    def delayed_session(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=3)
        return original(*args, **kwargs)
    ledger.new_session = delayed_session

    async def scenario():
        scope=anyio.CancelScope()
        async def request():
            with scope:
                await bridge_for(ledger).acall('checked_add_recorded',arguments('new-session'))
                raise AssertionError('cancelled request returned')
        async with anyio.create_task_group() as group:
            group.start_soon(request)
            await eventually(entered)
            scope.cancel()
            release.set()
    anyio.run(scenario)
    assert rows(ledger,'new-session') == []
