"""MCP tool business handler tests without the unavailable SDK."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from verified_execution.core import LocalAuthority
from verified_execution.worker_leases import LeasedLedger, LeasedToolBridge
from verified_execution.ledger import LedgerError, OperationLedger
import pytest


def test_tool_replays_from_two_worker_instances(tmp_path):
    db=tmp_path/'work.sqlite3'
    a=LeasedToolBridge(LeasedLedger(db),principal='alice',authority=LocalAuthority.calculator_only())
    b=LeasedToolBridge(LeasedLedger(db),principal='alice',authority=LocalAuthority.calculator_only())
    data={'request_key':'one','left':20,'right':22}
    first=a.call('checked_add_recorded',data)
    second=b.call('checked_add_recorded',data)
    assert first.ok and second.ok and second.replayed and second.value==42
    assert first.operation_id==second.operation_id
    assert LeasedLedger(db).history(first.operation_id).count('leased_dispatch')==1


def test_tool_race_does_not_double_dispatch(tmp_path):
    db=tmp_path/'work.sqlite3';a=LeasedToolBridge(LeasedLedger(db),principal='alice',authority=LocalAuthority.calculator_only())
    b=LeasedToolBridge(LeasedLedger(db),principal='alice',authority=LocalAuthority.calculator_only())
    def action(i):
        return (a if i%2 else b).call('checked_add_recorded',{'request_key':'race','left':1,'right':2})
    with ThreadPoolExecutor(max_workers=12) as pool:
        results=list(pool.map(action,range(24)))
    assert sum(not r.replayed for r in results)==1
    assert all(r.ok or r.error_code=='IN_PROGRESS' for r in results)
    first=next(r for r in results if not r.replayed)
    assert LeasedLedger(db).history(first.operation_id).count('leased_dispatch')==1


def test_tool_invalid_and_denied(tmp_path):
    db=tmp_path/'work.sqlite3'; x=LeasedToolBridge(LeasedLedger(db),principal='alice',authority=LocalAuthority(frozenset()))
    assert x.call('checked_add_recorded',{'request_key':'x','left':1,'right':2}).error_code=='NOT_AUTHORIZED'
    assert x.call('checked_add_recorded',{'request_key':'x','left':1,'right':2,'admin':True}).error_code=='INVALID_ARGUMENT'
    assert x.call('checked_add_recorded',{'request_key':True,'left':1,'right':2}).error_code=='INVALID_REQUEST_KEY'
    assert x.call('unknown',{}).error_code=='UNKNOWN_TOOL'


def test_legacy_instance_cannot_mutate_leased_operation(tmp_path):
    db=tmp_path/'work.sqlite3'; leased=LeasedLedger(db);legacy=OperationLedger(db)
    owner=leased.new_session()
    op=leased.reserve(principal='alice',request_key='reserved',tool_name='checked_add',
        target_id=None,resource_version=None,arguments={'left':1,'right':2},requires_approval=False)
    leased.start(op.operation_id,token=owner.token)
    for callback in [lambda:legacy.mark_unknown(op.operation_id),
                     lambda:legacy.complete_calculation(op.operation_id,3),
                     lambda:legacy.record_definitive_result(op.operation_id,'succeeded')]:
        with pytest.raises(LedgerError,match='OWNER_REQUIRED'):
            callback()
    assert legacy.recover_interrupted()==0
    assert legacy.get(op.operation_id).state=='dispatched'
    leased.finish(op.operation_id,token=owner.token,value=3)
