"""Proof-by-tests of finite lease state model; NOT protocol or formal proof."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest

from verified_execution.core import LocalAuthority
from verified_execution.ledger import LedgerError
from verified_execution.worker_leases import LeasedLedger, LeasedCalculator


def reserved(ledger: LeasedLedger, key: str):
    return ledger.reserve(principal='host',request_key=key,tool_name='checked_add',
                          target_id=None,resource_version=None,arguments={'left':1,'right':2},
                          requires_approval=False)


def reject(code, fun, *args, **kwargs):
    with pytest.raises(LedgerError) as exc:
        fun(*args,**kwargs)
    assert exc.value.code == code


def test_live_worker_unchanged_by_other_worker_recovery(tmp_path):
    path=tmp_path/'ledger.sqlite3'; a=LeasedLedger(path); b=LeasedLedger(path)
    worker=a.new_session(duration_ms=5000)
    op=reserved(a,'logical-1')
    a.start(op.operation_id,token=worker.token)
    assert b.recover_expired()==0
    assert b.get(op.operation_id).state=='dispatched'
    a.finish(op.operation_id,token=worker.token,value=3)
    assert b.get(op.operation_id).state=='succeeded'
    assert b.read_calculation(op.operation_id,principal='host')==3


def test_expired_lease_quarantines_and_stale_owner_cannot_finish(tmp_path):
    a=LeasedLedger(tmp_path/'ledger.sqlite3');b=LeasedLedger(a.path)
    token=a.new_session(duration_ms=70).token
    op=reserved(a,'will-expire');a.start(op.operation_id,token=token,duration_ms=70)
    time.sleep(.11)
    assert b.recover_expired()==1
    assert b.get(op.operation_id).state=='unknown'
    reject('STALE_OR_DIFFERENT_OWNER',a.finish,op.operation_id,token=token,value=3)
    assert b.recover_expired()==0
    assert b.read_calculation(op.operation_id,principal='host') is None


def test_expired_owner_cannot_heartbeat_or_dispatch(tmp_path):
    db=LeasedLedger(tmp_path/'ledger.sqlite3')
    token=db.new_session(duration_ms=70).token
    time.sleep(.11)
    reject('SESSION_EXPIRED',db.heartbeat,token)
    op=reserved(db,'expired')
    reject('SESSION_EXPIRED',db.start,op.operation_id,token=token)
    assert db.get(op.operation_id).state=='prepared'


def test_heartbeat_preserves_active_lease(tmp_path):
    db=LeasedLedger(tmp_path/'ledger.sqlite3');token=db.new_session(duration_ms=250).token
    op=reserved(db,'heartbeat');db.start(op.operation_id,token=token,duration_ms=250)
    time.sleep(.12)
    db.heartbeat(token,duration_ms=300)
    time.sleep(.18)
    assert db.recover_expired()==0
    assert db.finish(op.operation_id,token=token,value=3).state=='succeeded'


def test_different_session_cannot_finish_or_mark_unknown(tmp_path):
    db=LeasedLedger(tmp_path/'ledger.sqlite3');a=db.new_session();b=db.new_session()
    op=reserved(db,'one');db.start(op.operation_id,token=a.token)
    reject('STALE_OR_DIFFERENT_OWNER',db.finish,op.operation_id,token=b.token,value=3)
    reject('STALE_OR_DIFFERENT_OWNER',db.mark_owner_unknown,op.operation_id,token=b.token)
    assert db.get(op.operation_id).state=='dispatched'


def test_old_unsafe_recovery_rejected_in_leased_mode(tmp_path):
    db=LeasedLedger(tmp_path/'ledger.sqlite3')
    reject('USE_EXPIRED_LEASE_RECOVERY',db.recover_interrupted)
    op=reserved(db,'old-path')
    reject('OWNER_REQUIRED',db.begin_dispatch,op.operation_id)
    reject('OWNER_REQUIRED',db.complete_calculation,op.operation_id,3)
    reject('OWNER_REQUIRED',db.record_definitive_result,op.operation_id,'succeeded')
    reject('OWNER_REQUIRED',db.mark_unknown,op.operation_id)


def test_lease_session_unique_and_no_reuse(tmp_path):
    db=LeasedLedger(tmp_path/'ledger.sqlite3')
    workers=[db.new_session() for _ in range(25)]
    assert len({w.token for w in workers})==25
    assert all(w.expires_at_ms > 0 for w in workers)


@pytest.mark.parametrize('duration',[-1,0,49,1.2,True,3600001])
def test_reject_bad_lease_duration(tmp_path,duration):
    db=LeasedLedger(tmp_path/'ledger.sqlite3')
    reject('INVALID_LEASE_DURATION',db.new_session,duration_ms=duration)


def test_recovery_does_not_rewrite_legacy_unleased_records(tmp_path):
    from verified_execution.ledger import OperationLedger
    path=tmp_path/'mixed.sqlite3'
    legacy=OperationLedger(path)
    op=legacy.reserve(principal='host',request_key='legacy',tool_name='checked_add',
       target_id=None,resource_version=None,arguments={'left':1,'right':2},requires_approval=False)
    legacy.begin_dispatch(op.operation_id)
    leased=LeasedLedger(path)
    assert leased.recover_expired()==0
    assert leased.get(op.operation_id).state=='dispatched'
    # In mixed-version deployment, legacy writes remain an unverified risk.


def test_parallel_clients_only_one_can_start(tmp_path):
    db=LeasedLedger(tmp_path/'ledger.sqlite3');a=db.new_session();b=db.new_session()
    op=reserved(db,'parallel')
    def attempt(token):
        local=LeasedLedger(db.path)
        try:
            local.start(op.operation_id,token=token)
            return token
        except LedgerError as exc:
            assert exc.code=='NOT_PREPARED';return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        values=list(pool.map(attempt,[a.token,b.token]))
    assert sum(v is not None for v in values)==1
    assert db.get(op.operation_id).state=='dispatched'
    winning=next(v for v in values if v is not None)
    assert db.finish(op.operation_id,token=winning,value=3).state=='succeeded'


def test_cancel_before_start_cannot_dispatch(tmp_path):
    db=LeasedLedger(tmp_path/'ledger.sqlite3');worker=db.new_session()
    op=reserved(db,'cancelled');db.request_cancel(op.operation_id)
    reject('NOT_PREPARED',db.start,op.operation_id,token=worker.token)
    assert db.get(op.operation_id).state=='cancelled'


def test_cancel_after_start_is_not_rollback(tmp_path):
    db=LeasedLedger(tmp_path/'ledger.sqlite3');worker=db.new_session()
    op=reserved(db,'cancel-late');db.start(op.operation_id,token=worker.token)
    stopped=db.request_cancel(op.operation_id)
    assert stopped.cancel_requested and stopped.state=='dispatched'
    db.finish(op.operation_id,token=worker.token,value=3)
    assert db.get(op.operation_id).state=='succeeded'


def test_simple_leased_calculator_replay(tmp_path):
    db=LeasedLedger(tmp_path/'ledger.sqlite3');worker=db.new_session()
    cal=LeasedCalculator(db,principal='host',authority=LocalAuthority.calculator_only(),session=worker)
    x=cal.execute(request_key='replay',name='checked_add',arguments={'left':10,'right':20})
    y=cal.execute(request_key='replay',name='checked_add',arguments={'left':10,'right':20})
    assert x.ok and y.ok and y.replayed and y.value==x.value==30
    assert db.history(x.operation_id).count('leased_dispatch')==1


def test_leased_calculator_denies_unauthorized(tmp_path):
    db=LeasedLedger(tmp_path/'ledger.sqlite3');w=db.new_session()
    cal=LeasedCalculator(db,principal='host',authority=LocalAuthority(frozenset()),session=w)
    result=cal.execute(request_key='bad',name='checked_add',arguments={'left':1,'right':2})
    assert result.error_code=='NOT_AUTHORIZED'
    with sqlite3.connect(db.path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM operations').fetchone()[0]==0


def test_multi_process_active_worker_protected(tmp_path):
    """One process owns a lease; second process recovers without touching it."""
    path=tmp_path/'multi.sqlite3'
    script='''
import sys,time
from verified_execution.worker_leases import LeasedLedger
x=LeasedLedger(sys.argv[1]); w=x.new_session(duration_ms=2000)
o=x.reserve(principal='host',request_key='real-subprocess',tool_name='checked_add',
    target_id=None,resource_version=None,arguments={'left':1,'right':2},requires_approval=False)
x.start(o.operation_id,token=w.token)
print(o.operation_id,flush=True)
time.sleep(0.5)
x.finish(o.operation_id,token=w.token,value=3)
'''
    env=os.environ.copy();env['PYTHONPATH']=str(Path(__file__).parents[1]/'src')
    child=subprocess.Popen([sys.executable,'-u','-c',script,str(path)],
                           env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        operation_id=child.stdout.readline().strip()
        assert operation_id
        second=LeasedLedger(path)
        assert second.recover_expired()==0
        assert second.get(operation_id).state=='dispatched'
        assert child.wait(timeout=4)==0,child.stderr.read()
        assert second.get(operation_id).state=='succeeded'
    finally:
        if child.poll() is None:
            child.kill();child.wait(timeout=2)
        child.stdout.close();child.stderr.close()


def test_fenced_definitive_failure_and_owner_check(tmp_path):
    db=LeasedLedger(tmp_path/'ledger.sqlite3')
    one=db.new_session();two=db.new_session()
    op=reserved(db,'failure')
    db.start(op.operation_id,token=one.token)
    reject('STALE_OR_DIFFERENT_OWNER',db.fail,op.operation_id,token=two.token)
    assert db.fail(op.operation_id,token=one.token).state=='failed'
    reject('NOT_DISPATCHED',db.finish,op.operation_id,token=one.token,value=3)


def test_pure_overflow_is_failed_not_unknown(tmp_path):
    db=LeasedLedger(tmp_path/'ledger.sqlite3')
    session=db.new_session()
    service=LeasedCalculator(db,principal='host',authority=LocalAuthority.calculator_only(),session=session)
    r=service.execute(request_key='overflow',name='checked_add',
                      arguments={'left':4294967295,'right':1})
    assert r.error_code=='OVERFLOW' and r.ledger_state=='failed'
    assert db.get(r.operation_id).state=='failed'
    assert db.history(r.operation_id)[-1]=='leased_definitive_failed'
