"""Actual separate-process lock and collision checks, finite workload.

Passes only within measured local durations and count; no infinite-load claim.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import pytest

from verified_execution.worker_leases import LeasedLedger

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('workers,iterations,keys', [(2,25,12), (4,30,18)])
def test_process_collisions_preserve_at_most_one_dispatch(tmp_path,workers,iterations,keys):
    path=tmp_path/'shared.sqlite3'
    env=dict(os.environ, PYTHONPATH=str(ROOT/'src'))
    commands=[[sys.executable,'-u',str(ROOT/'scripts'/'multiprocess_stress_worker.py'),
               str(path),str(keys),str(iterations),str(i)] for i in range(workers)]
    procs=[subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)
           for cmd in commands]
    try:
        results=[]
        for proc in procs:
            stdout,stderr=proc.communicate(timeout=25)
            assert proc.returncode==0,stderr.decode(errors='replace')
            results.append(json.loads(stdout))
    finally:
        for proc in procs:
            if proc.poll() is None:proc.kill()
            proc.wait(timeout=5)
    assert sum(x['total'] for x in results) == workers*iterations
    assert all(not x['other'] for x in results),results
    ledger=LeasedLedger(path)
    with sqlite3.connect(path) as connection:
        all_operations=connection.execute(
            'SELECT operation_id,request_key,state FROM operations').fetchall()
        assert len(all_operations)==keys
        assert all(row[2]=='succeeded' for row in all_operations)
        events=connection.execute(
            "SELECT operation_id,COUNT(*) FROM events WHERE event_name='leased_dispatch' "
            "GROUP BY operation_id").fetchall()
        assert len(events)==keys
        assert all(count==1 for _,count in events)
        assert connection.execute('PRAGMA integrity_check').fetchone()==('ok',)
    for operation_id, key, state in all_operations:
        index = int(key.removeprefix('shared-'))
        assert ledger.read_calculation(operation_id, principal='multiprocess-test') == 1000 + index


def test_same_request_key_different_values_rejected(tmp_path):
    ledger=LeasedLedger(tmp_path/'shared.sqlite3')
    first=ledger.reserve(principal='host',request_key='stable-key',tool_name='checked_add',
                         target_id=None,resource_version=None,
                         arguments={'left':1,'right':2},requires_approval=False)
    from verified_execution.ledger import LedgerError
    with pytest.raises(LedgerError, match='IDEMPOTENCY_CONFLICT'):
        ledger.reserve(principal='host',request_key='stable-key',tool_name='checked_add',
                       target_id=None,resource_version=None,
                       arguments={'left':2,'right':2},requires_approval=False)
    assert ledger.get(first.operation_id).state=='prepared'
