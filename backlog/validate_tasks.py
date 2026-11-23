#!/usr/bin/env python3
"""Task ledger structure and evidence-check proof, not Rust/MCP runtime verification."""
from pathlib import Path
import json, collections
P=Path(__file__).resolve().parent
REPOSITORY_ROOT=P.parent

def evidence_matches_task(task):
    """EVIDENCE-001: accept only local matching task evidence, not arbitrary files.

    This verifies structural provenance, not the authenticity of old test runs.
    """
    if not task["evidence_paths"]:
        return False
    for item in task["evidence_paths"]:
        if type(item) is not str:
            return False
        relative = Path(item)
        if (relative.is_absolute() or ".." in relative.parts
                or not relative.parts or relative.parts[0] != "evidence"):
            return False
        file_path = (REPOSITORY_ROOT / relative).resolve()
        if not file_path.is_relative_to(REPOSITORY_ROOT) or not file_path.is_file():
            return False
        try:
            proof = json.loads(file_path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError, OSError):
            return False
        if (proof.get("task_id") != task["task_id"]
                or proof.get("verification_status") != task["verification_status"]
                or proof.get("executed") is not True):
            return False
    return True

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
        assert evidence_matches_task(task), task['task_id']
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
