"""Independent offline check that new done statuses have actual matching evidence.

STATE: READ_TASKS -> READ_JUNIT -> VALIDATE_ALL_TASK_PROOFS -> VERIFIED | REJECTED.
This checks file integrity and recorded local tests; it cannot cryptographically
prove that a JUnit file was produced by a trusted outside executor.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]

class EvidenceError(ValueError):
    pass

def verify_one(task: dict, record: dict, case_names: set[str], root: Path=ROOT) -> None:
    if record.get('task_id')!=task['task_id'] or record.get('acceptance_criteria')!=task['acceptance_criteria']:
        raise EvidenceError('wrong task identity or acceptance: '+task['task_id'])
    if record.get('verification_status')!='passed' or record.get('executed') is not True:
        raise EvidenceError('not actually executed: '+task['task_id'])
    name=record.get('test_name')
    if name not in case_names or not name.startswith('test_scope_'+task['task_id'].split('.')[-1]+'_'):
        raise EvidenceError('missing or unrelated passing test: '+task['task_id'])
    if record.get('test_suite')!='evidence/scope_sprint.xml':
        raise EvidenceError('unexpected test suite: '+task['task_id'])
    sources=record.get('source_sha256')
    if not isinstance(sources,dict) or not sources:
        raise EvidenceError('no source hashes')
    for name,digest in sources.items():
        if not isinstance(name,str) or not isinstance(digest,str):
            raise EvidenceError('malformed source assertion')
        path=(root/name).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            raise EvidenceError('untrusted/missing source path')
        if hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
            raise EvidenceError('source content changed: '+name)

def run(root:Path=ROOT) -> dict:
    items=[json.loads(s) for s in (root/'tasks.jsonl').read_text().splitlines() if s]
    updated=[x for x in items if x['task_id'].startswith('task.scope.') and x['task_id'].split('.')[-1] in
             ('001','003','004','006','009','010','011','012','014','015','018')]
    event_items=[json.loads(s) for s in (root/'progress_events.jsonl').read_text().splitlines() if s]
    events={e['task_id']:e for e in event_items}
    if len(events)!=len(event_items) or len(events)!=11:
        raise EvidenceError('duplicate/missing progress events')
    suite=ET.parse(root/'evidence/scope_sprint.xml').getroot()
    suites=suite.findall('testsuite') if suite.tag!='testsuite' else [suite]
    if len(suites)!=1:
        raise EvidenceError('ambiguous test suites')
    s=suites[0]
    if any(int(s.get(k,'0')) for k in ('errors','failures','skipped')):
        raise EvidenceError('test suite is not fully passed')
    successes=set()
    for case in s.findall('testcase'):
        if case.find('failure') is not None or case.find('skipped') is not None or case.find('error') is not None:
            continue
        successes.add(case.get('name'))
    passed=0; blocked=0
    for t in updated:
        ev=events.get(t['task_id'])
        if ev is None or ev['to_status']!=t['status'] or ev['evidence_paths']!=t['evidence_paths']:
            raise EvidenceError('progress event mismatch')
        if t['status']=='blocked':
            if t['task_id']!='task.scope.015' or t['evidence_paths'] or t['verification_status']=='passed':
                raise EvidenceError('false blocked completion')
            blocked+=1;continue
        if t['status']!='done' or t['verification_status']!='passed' or len(t['evidence_paths'])!=1:
            raise EvidenceError('false completion state')
        evidence_path=(root/t['evidence_paths'][0]).resolve()
        if not evidence_path.is_relative_to(root.resolve()) or not evidence_path.is_file():
            raise EvidenceError('evidence path escaped project')
        record=json.loads(evidence_path.read_text())
        verify_one(t,record,successes,root)
        passed+=1
    return {'verified':True,'updated_tasks':len(updated),'newly_done':passed,'blocked':blocked,
            'scope_junit_tests':int(s.get('tests','0')),'accepted_test_names':len(successes),
            'assurance':'local source digests and recorded tests; official MCP SDK not connected'}

if __name__=='__main__':
    print(json.dumps(run(),ensure_ascii=False,indent=2))
