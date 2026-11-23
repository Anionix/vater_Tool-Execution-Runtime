"""Version 0.11.0 evidence aggregator; counts only executed JUnit test cases.

STATE: COLLECT -> VERIFY -> CLASSIFY -> REPORT.
Do not promote a skipped official SDK case to a pass.
"""
from __future__ import annotations
from datetime import datetime,timezone
from hashlib import sha256
import importlib.metadata
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
EVIDENCE=ROOT/'evidence'
SUITES={'baseline':'baseline_0_11.xml','ledger_and_cancellation':'ledger_0_11.xml',
        'process_lifecycle':'lifecycle_0_11.xml'}


def read_suite(filename):
    path=EVIDENCE/filename
    document=ET.parse(path).getroot()
    suite=document.find('testsuite') if document.tag=='testsuites' else document
    if suite is None:raise ValueError('Missing testsuite')
    counts={key:int(suite.attrib[key]) for key in ('tests','failures','errors','skipped')}
    counts['passed']=counts['tests']-counts['failures']-counts['errors']-counts['skipped']
    if counts['passed']<0:raise ValueError('Inconsistent result count')
    counts['duration_seconds']=float(suite.attrib.get('time','0'))
    counts['sha256']=sha256(path.read_bytes()).hexdigest()
    return counts


def main():
    suites={name:read_suite(path) for name,path in SUITES.items()}
    totals={key:sum(s[key] for s in suites.values()) for key in
            ('tests','passed','failures','errors','skipped')}
    benchmark=json.loads((EVIDENCE/'multiprocess_stress.json').read_text())
    assert benchmark['overall']=='PASS' and benchmark['total_requests']==1080
    assert totals=={'tests':303,'passed':289,'failures':0,'errors':0,'skipped':14},totals
    sdk=None
    try:sdk=importlib.metadata.version('mcp')
    except importlib.metadata.PackageNotFoundError:pass
    tasks=[
      {'task':'WORK-010-001','status':'SKIPPED_EXTERNAL_DEPENDENCY','reason':'Official Python MCP SDK unavailable locally'},
      {'task':'WORK-010-002','status':'SKIPPED_EXTERNAL_DEPENDENCY','reason':'Real official MCP client/server cancellation cannot be executed'},
      {'task':'WORK-010-003','status':'VERIFIED_BOUNDED_ONLY','reason':'1080 requests, 6 worker processes x 60 per trial x 3, plus unit tests; not a multi-day soak test'},
      {'task':'WORK-010-004','status':'IMPLEMENTED_LOCAL','reason':'POSIX fsync cancellation journal, locked SQLite recovery, process crash, startup replay and access checks tested'},
      {'task':'WORK-010-005','status':'SKIPPED_EXTERNAL_DEPENDENCY','reason':'No real mutation-capable provider or authoritative status endpoint to reconcile'},
      {'task':'WORK-010-006','status':'SKIPPED_INDEPENDENT_REVIEW','reason':'Local negative tests are not independent security review or real identity authentication'},
      {'task':'WORK-010-007','status':'SKIPPED_TOOLCHAIN','reason':'No Rust compiler available; cross-language execution not possible'},
    ]
    summary={'report_identifier':'mcp-2026-11-23-python-feasible-0.11.0',
             'generated_at_utc':datetime.now(timezone.utc).isoformat(),
             'python':sys.version.split()[0],
             'suites':suites,'totals':totals,
             'stress':{'runs':len(benchmark['outcomes']),
                       'requests':benchmark['total_requests'],
                       'distinct_operations_per_run':30,
                       'max_elapsed_seconds':max(o['elapsed_seconds'] for o in benchmark['outcomes']),
                       'evidence':'evidence/multiprocess_stress.json'},
             'sdk':{'installed_version':sdk,'real_sdk_tests_executed':0,'skipped':14},
             'task_decisions':tasks,
             'limitations':['short duration load does not verify multi-day availability',
                            'journal requires trusted POSIX filesystem, successful file and directory fsync',
                            'SQLite and journal storage can both fail; no durable guarantee then',
                            'remote side effects and real MCP transport not tested',
                            'approval identity issuance and independent audit not performed',
                            'single full combined pytest invocation timed out; separate suite executions passed'],
             'release_decision':'BLOCKED'}
    (EVIDENCE/'verification_0_11.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps({'totals':totals,'stress_requests':benchmark['total_requests'],
                      'release_decision':summary['release_decision']}))

if __name__=='__main__':main()
