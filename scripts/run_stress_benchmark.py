"""Finite, reproducible SQLite multiprocess stress benchmark. NOT soak testing.

Machine contract STRESS-010:
- N distinct logical operations => exactly N final successful results
- dispatch event count exactly N, every event occurs once
- no SQLITE_BUSY, unexpected error, or SQLite integrity error in tested load
- report real observed elapsed times; never interpolate unrun durations
"""
from __future__ import annotations
import argparse
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
from verified_execution.worker_leases import LeasedLedger

ROOT=Path(__file__).resolve().parents[1]


def run_one(folder:Path,trial:int,workers:int,iterations:int,keys:int,timeout:float)->dict:
    db=folder/f'trial-{trial}.sqlite3'
    env=dict(os.environ, PYTHONPATH=str(ROOT/'src'))
    commands=[[sys.executable,'-u',str(ROOT/'scripts'/'multiprocess_stress_worker.py'),
               str(db),str(keys),str(iterations),str(i)] for i in range(workers)]
    start=time.monotonic()
    procs=[subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)
           for command in commands]
    outputs=[]
    try:
        for process in procs:
            out,err=process.communicate(timeout=timeout)
            if process.returncode!=0:
                raise RuntimeError('worker_failure:'+err.decode(errors='replace')[-400:])
            outputs.append(json.loads(out))
    finally:
        for process in procs:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
    elapsed=time.monotonic()-start
    with sqlite3.connect(db) as conn:
        operations=conn.execute('SELECT operation_id,request_key,state FROM operations').fetchall()
        event_counts=dict(conn.execute("SELECT operation_id,COUNT(*) FROM events "
                                       "WHERE event_name='leased_dispatch' GROUP BY operation_id"))
        integrity=conn.execute('PRAGMA integrity_check').fetchone()[0]
    ledger=LeasedLedger(db)
    correct=(len(operations)==keys and len(event_counts)==keys and integrity=='ok'
             and sum(r['total'] for r in outputs)==workers*iterations
             and all(not r['other'] for r in outputs))
    for operation_id,key,state in operations:
        ix=int(key.removeprefix('shared-'))
        correct &= state=='succeeded' and event_counts.get(operation_id)==1
        correct &= ledger.read_calculation(operation_id,principal='multiprocess-test')==1000+ix
    result={'trial':trial,'workers':workers,'requests_per_worker':iterations,'request_total':workers*iterations,
            'distinct_operations_expected':keys,'distinct_operations_actual':len(operations),
            'success_or_replay':sum(r['ok'] for r in outputs),
            'in_progress':sum(r['in_progress'] for r in outputs),
            'errors':[r['other'] for r in outputs if r['other']],
            'sqlite_integrity':integrity, 'dispatch_counts_exact':correct,
            'elapsed_seconds':round(elapsed,4),'status':'PASS' if correct else 'FAIL'}
    if not correct:
        raise RuntimeError('stress_invariant_violation:'+json.dumps(result))
    return result


def main()->int:
    parser=argparse.ArgumentParser()
    parser.add_argument('--trials',type=int,default=3)
    parser.add_argument('--workers',type=int,default=6)
    parser.add_argument('--iterations',type=int,default=60)
    parser.add_argument('--keys',type=int,default=30)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    if not 1<=args.trials<=10 or not 1<=args.workers<=16 or not 1<=args.iterations<=1000 or not 1<=args.keys<=256:
        parser.error('bounds exceeded')
    with tempfile.TemporaryDirectory(prefix='mcp-stress-') as folder:
        outcomes=[run_one(Path(folder),i,args.workers,args.iterations,args.keys,30)
                  for i in range(args.trials)]
    report={'report_identifier':'STRESS-010','generated_at_utc':datetime.now(timezone.utc).isoformat(),
            'kind':'finite_local_multiprocess_test_not_soak_test','outcomes':outcomes,
            'total_requests':sum(x['request_total'] for x in outcomes),
            'overall':'PASS' if all(x['status']=='PASS' for x in outcomes) else 'FAIL'}
    dest=Path(args.output)
    dest.parent.mkdir(parents=True,exist_ok=True)
    dest.write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    print(json.dumps({'total_requests':report['total_requests'],'overall':report['overall'],
                      'max_elapsed_seconds':max(x['elapsed_seconds'] for x in outcomes)},ensure_ascii=False))
    return 0 if report['overall']=='PASS' else 1


if __name__=='__main__':
    raise SystemExit(main())
