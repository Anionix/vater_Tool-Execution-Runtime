"""Crash, SQLite lock and expired-owner cancellation intent tests.

Tests do NOT claim external effects or MCP network cancellation semantics.
"""
from pathlib import Path
import json
import sqlite3
import threading
import time

import anyio
import pytest

from verified_execution.cancellation_journal import CancellationJournal, JournalError
from verified_execution.cancellable_bridge import CancellableLeasedToolBridge
from verified_execution.core import LocalAuthority
from verified_execution.worker_leases import LeasedLedger


def operation(ledger, key='one'):
    return ledger.reserve(principal='host', request_key=key,
                          tool_name='checked_add', target_id=None,
                          resource_version=None, arguments={'left': 1, 'right': 2},
                          requires_approval=False)


def test_journal_is_durable_and_idempotent(tmp_path):
    ledger = LeasedLedger(tmp_path/'data.sqlite3')
    op = operation(ledger)
    store = CancellationJournal(tmp_path/'intents')
    store.persist(op.operation_id, principal='host')
    store.persist(op.operation_id, principal='host')
    assert len(store.list_intents()) == 1
    assert (tmp_path/'intents').stat().st_mode & 0o077 == 0
    assert store.replay(ledger)['applied_cancelled'] == 1
    assert ledger.get(op.operation_id).state == 'cancelled'
    assert store.replay(ledger)['applied_cancelled'] == 0
    assert store.list_intents() == []


def test_cancel_db_busy_remains_in_journal_then_recovers(tmp_path):
    ledger = LeasedLedger(tmp_path/'data.sqlite3', timeout_seconds=.07)
    op = operation(ledger)
    store = CancellationJournal(tmp_path/'intents')
    store.persist(op.operation_id, principal='host')
    started, release = threading.Event(), threading.Event()
    def hold_lock():
        with sqlite3.connect(ledger.path, isolation_level=None) as db:
            db.execute('BEGIN IMMEDIATE')
            started.set()
            assert release.wait(3)
            db.execute('COMMIT')
    worker = threading.Thread(target=hold_lock)
    worker.start()
    assert started.wait(2)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ledger.request_cancel(op.operation_id)
        with pytest.raises(sqlite3.OperationalError):
            store.replay(ledger)
        assert len(store.list_intents()) == 1
    finally:
        release.set()
        worker.join(timeout=3)
    assert store.replay(ledger)['applied_cancelled'] == 1
    assert ledger.get(op.operation_id).state == 'cancelled'


def test_running_live_owner_is_never_overwritten(tmp_path):
    ledger = LeasedLedger(tmp_path/'data.sqlite3')
    session = ledger.new_session(duration_ms=280)
    op = operation(ledger)
    ledger.start(op.operation_id, token=session.token, duration_ms=280)
    store = CancellationJournal(tmp_path/'intents')
    store.persist(op.operation_id, principal='host')
    assert store.replay(ledger)['pending_owner'] == 1
    assert ledger.get(op.operation_id).state == 'dispatched'
    assert len(store.list_intents()) == 1
    time.sleep(.31)
    assert store.replay(ledger)['expired_unknown'] == 1
    assert ledger.get(op.operation_id).state == 'unknown'
    assert store.list_intents() == []


def test_success_stays_success_after_late_cancel(tmp_path):
    ledger = LeasedLedger(tmp_path/'data.sqlite3')
    session = ledger.new_session()
    op = operation(ledger)
    ledger.start(op.operation_id, token=session.token)
    ledger.finish(op.operation_id, token=session.token, value=3)
    store = CancellationJournal(tmp_path/'intents')
    store.persist(op.operation_id, principal='host')
    assert store.replay(ledger)['already_terminal'] == 1
    assert ledger.get(op.operation_id).state == 'succeeded'
    assert ledger.read_calculation(op.operation_id, principal='host') == 3


def test_other_principal_cannot_apply_forged_intent(tmp_path):
    ledger = LeasedLedger(tmp_path/'data.sqlite3')
    op = operation(ledger)
    store = CancellationJournal(tmp_path/'intents')
    store.persist(op.operation_id, principal='other-host')
    with pytest.raises(JournalError, match='PRINCIPAL_MISMATCH'):
        store.replay(ledger)
    assert ledger.get(op.operation_id).state == 'prepared'
    assert len(store.list_intents()) == 1


def test_corrupted_intent_is_not_silently_deleted(tmp_path):
    store = CancellationJournal(tmp_path/'intents')
    f = tmp_path/'intents'/('0'*64+'.json')
    f.write_text('{bad json', encoding='utf-8')
    f.chmod(0o600)
    with pytest.raises(JournalError, match='CORRUPTED_INTENT_FILE'):
        store.list_intents()
    assert f.exists()


def test_non_private_journal_directory_is_rejected(tmp_path):
    directory = tmp_path/'bad'
    directory.mkdir(mode=0o777)
    directory.chmod(0o755)
    with pytest.raises(JournalError, match='UNTRUSTED_JOURNAL_DIRECTORY'):
        CancellationJournal(directory)


def test_nonexistent_operation_intent_kept_pending(tmp_path):
    ledger = LeasedLedger(tmp_path/'data.sqlite3')
    store = CancellationJournal(tmp_path/'intents')
    store.persist('not-created-yet', principal='host')
    assert store.replay(ledger)['not_found'] == 1
    assert len(store.list_intents()) == 1


def test_async_cancel_cleanup_db_failure_keeps_intent(tmp_path):
    ledger = LeasedLedger(tmp_path/'data.sqlite3')
    store = CancellationJournal(tmp_path/'intents')
    entered = anyio.Event()
    proceed = anyio.Event()
    original = ledger.request_cancel
    def broken_cancel(operation_id):
        raise sqlite3.OperationalError('database is locked')
    ledger.request_cancel = broken_cancel

    async def pause(_op):
        entered.set()
        await proceed.wait()

    bridge = CancellableLeasedToolBridge(
        ledger, principal='host', authority=LocalAuthority.calculator_only(),
        before_dispatch=pause, cancellation_journal=store)

    async def scenario():
        scope = anyio.CancelScope()
        async def invoke():
            with scope:
                await bridge.acall('checked_add_recorded',
                                   {'request_key': 'cancelled-while-busy', 'left':1,'right':2})
        async with anyio.create_task_group() as tg:
            tg.start_soon(invoke)
            await entered.wait()
            scope.cancel()
            proceed.set()

    anyio.run(scenario)
    ledger.request_cancel = original
    pending = store.list_intents()
    assert len(pending) == 1
    op = ledger.get(pending[0]['operation_id'])
    assert op.state == 'prepared'
    assert store.replay(ledger)['applied_cancelled'] == 1
    assert ledger.get(op.operation_id).state == 'cancelled'


def test_repeated_recovery_has_no_duplicate_events(tmp_path):
    ledger = LeasedLedger(tmp_path/'data.sqlite3')
    op = operation(ledger)
    store = CancellationJournal(tmp_path/'intents')
    store.persist(op.operation_id, principal='host')
    store.replay(ledger)
    store.replay(ledger)
    assert ledger.history(op.operation_id).count('cancelled_before_dispatch') == 1


def test_recovery_command_in_separate_process(tmp_path):
    import subprocess
    import os
    import sys
    db=tmp_path/'data.sqlite3'
    ledger=LeasedLedger(db)
    op=operation(ledger)
    journal=CancellationJournal(tmp_path/'intents')
    journal.persist(op.operation_id, principal='host')
    root=Path(__file__).resolve().parents[1]
    done=subprocess.run([sys.executable, str(root/'scripts'/'recover_cancellation_intents.py'),
                         '--database',str(db),'--journal',str(tmp_path/'intents')],
                        capture_output=True,timeout=5,
                        env=dict(os.environ,PYTHONPATH=str(root/'src')))
    assert done.returncode==0,done.stderr
    assert json.loads(done.stdout)['applied_cancelled']==1
    assert ledger.get(op.operation_id).state=='cancelled'


def test_recovery_command_reports_live_owner_pending(tmp_path):
    import subprocess
    import os
    import sys
    db=tmp_path/'data.sqlite3'
    ledger=LeasedLedger(db)
    worker=ledger.new_session(duration_ms=3000)
    op=operation(ledger)
    ledger.start(op.operation_id,token=worker.token)
    journal=CancellationJournal(tmp_path/'intents')
    journal.persist(op.operation_id, principal='host')
    root=Path(__file__).resolve().parents[1]
    result=subprocess.run([sys.executable,str(root/'scripts'/'recover_cancellation_intents.py'),
                           '--database',str(db),'--journal',str(tmp_path/'intents')],
                          capture_output=True,timeout=5,
                          env=dict(os.environ,PYTHONPATH=str(root/'src')))
    assert result.returncode==2
    assert json.loads(result.stdout)['pending_owner']==1
    assert ledger.get(op.operation_id).state=='dispatched'


def test_directory_sync_failure_never_reports_durable_success(tmp_path, monkeypatch):
    """The journal writer raises on an fsync fault, no success claim is issued."""
    store = CancellationJournal(tmp_path/'intents')
    def fail_sync():
        raise OSError('simulated storage failure')
    monkeypatch.setattr(store, '_sync_directory', fail_sync)
    with pytest.raises(OSError, match='simulated storage failure'):
        store.persist('intent-with-failed-dir-sync', principal='host')
    # It may be visible now but is not guaranteed to survive a real power loss.
    # The test does NOT infer durability from the presence of the file.


def test_symlinked_journal_file_is_not_trusted(tmp_path):
    store=CancellationJournal(tmp_path/'intents')
    outside=tmp_path/'outside.json'
    outside.write_text('{}',encoding='utf-8')
    forged=store.directory / ('0'*64+'.json')
    forged.symlink_to(outside)
    with pytest.raises(JournalError, match='UNTRUSTED_INTENT_FILE'):
        store.list_intents()
    assert outside.read_text()=='{}'
