#!/usr/bin/env python3
"""Task ledger structure and evidence-check proof, not Rust/MCP runtime verification."""
from pathlib import Path
import json, collections
P=Path(__file__).resolve().parent
REPOSITORY_ROOT=P.parent
items=[json.loads(x) for x in (P/'tasks.jsonl').read_text(encoding='utf-8').splitlines() if x]
ids=[x['task_id'] for x in items]
assert len(ids)==len(set(ids)), 'duplicate task identifier'
all_ids=set(ids)
sources={json.loads(x)['source_id'] for x in (REPOSITORY_ROOT/'docs'/'sources.jsonl').read_text(encoding='utf-8').splitlines() if x}
for task in items:
    assert task['task_id'].startswith(('task.','gate.'))
    assert len(task['acceptance_criteria'])>=8 and len(task['title'])>=4
    assert task['acceptance_criteria'].strip()
    assert set(task['depends_on']).issubset(all_ids),task['task_id']
    assert task['task_id'] not in task['depends_on']
    assert set(task['source_ids']).issubset(sources)
    assert task['status'] in ['not_started','in_progress','blocked','review','done']
    assert task['verification_status'] in ['not_executed','failed','passed','not_applicable']
    if task['verification_status'] in ['passed','not_applicable']:
        assert task['evidence_paths'] and all((REPOSITORY_ROOT/e).resolve().is_relative_to(REPOSITORY_ROOT.resolve()) and (REPOSITORY_ROOT/e).is_file() for e in task['evidence_paths']), task['task_id']
    if task['status']=='done':
        assert task['verification_status'] in ['passed','not_applicable']
# detect dependency cycles by depth first search
by_id={x['task_id']:x for x in items}; visited=set(); active=set()
for task in items:
    if task['status']=='done':
        assert all(by_id[d]['status']=='done' for d in task['depends_on']), 'unfinished dependency: '+task['task_id']
def visit(key):
    if key in active:raise AssertionError('dependency cycle: '+key)
    if key in visited:return
    active.add(key)
    for dependency in by_id[key]['depends_on']:visit(dependency)
    active.remove(key);visited.add(key)
for item in items:visit(item['task_id'])
try:
    import jsonschema
except ImportError:
    pass
else:
    schema=json.loads((P/'task.schema.json').read_text(encoding='utf-8'))
    for entry in items:jsonschema.validate(entry,schema)
print(json.dumps({'valid':True,'total_records':len(items),'atomic_tasks':sum(x['task_id'].startswith('task.') for x in items),'gates':sum(x['task_id'].startswith('gate.') for x in items),'completed':sum(x['status']=='done' for x in items),'verification_passed':sum(x['verification_status']=='passed' for x in items)},ensure_ascii=False))
