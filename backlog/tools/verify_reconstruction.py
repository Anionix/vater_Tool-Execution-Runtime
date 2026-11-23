"""Check that every migrated task equals the original task plus logged changes.

Contract: provenance(original) + version tag + migration_events -> revised record.
Never update the ledger or declare task completion during this operation.
"""
from __future__ import annotations
import json
from pathlib import Path
from validate_redesign import jsonl

ROOT=Path(__file__).resolve().parents[1]


def reconstruct() -> list[dict]:
    original=jsonl(ROOT/'history/prior_tasks.jsonl')
    events={e['task_id']:e for e in jsonl(ROOT/'migration_events.jsonl')}
    items=[]
    first_phases={
      'scope','trust','contracts','runtime','mcp_transport','authorization',
      'read_only','minimum_validation','minimum_release',
    }
    for old in original:
        item=old.copy()
        phase=item['phase']
        item['plan_version']='2.0.0-python-first'
        item['implementation_stage']=('python_minimum' if phase in first_phases else
                                      'optional_formal_rust' if phase=='formal_verification' else 'post_minimum')
        if item['task_id'] in events:
            ev=events[item['task_id']]
            for name,diff in ev['changes'].items():
                if name=='verification_reset':
                    item['verification_status']='not_executed'
                    item['evidence_paths']=[]
                else:
                    assert item.get(name)==diff['before'], (item['task_id'],name)
                    item[name]=diff['after']
            item['status']=ev['new_status']
        items.append(item)
    # Reapply separately logged progress after the design migration.
    progress={e['task_id']:e for e in jsonl(ROOT/'progress_events.jsonl')}
    assert len(progress)==len(jsonl(ROOT/'progress_events.jsonl'))
    for item in items:
        ev=progress.get(item['task_id'])
        if ev:
            assert item['status']==ev['from_status'], item['task_id']
            assert item['verification_status']==ev['from_verification'], item['task_id']
            item['status']=ev['to_status']
            item['verification_status']=ev['to_verification']
            item['evidence_paths']=ev['evidence_paths']
    return items


def main() -> None:
    actual=jsonl(ROOT/'tasks.jsonl')
    recomputed=reconstruct()
    assert actual==recomputed,'Unlogged task mutation or incomplete migration event'
    print(json.dumps({'verified':True,'reconstructed_tasks':len(actual),
                      'migration_events':len(jsonl(ROOT/'migration_events.jsonl')), 'progress_events':len(jsonl(ROOT/'progress_events.jsonl'))}))


if __name__=='__main__':main()
