"""Independent, standard-library-only validator for Python-first plan 2.0.0.

Machine contract / task-state machine:
  original -> (unchanged | migrated) -> (needs_new_evidence | retained_pass)
  -> prerequisite closure -> (blocked | ready) -> evidence-based completion.

A Python implementation or a passing test does not automatically mark an
unrelated task done. Rewrites clear old evidence and verification if formerly
passed. This audits *plan structure*, NOT runtime security or SDK compatibility.
"""
from __future__ import annotations
from collections import Counter, defaultdict, deque
import json
from pathlib import Path

BASE_PHASES = (
    'scope','trust','contracts','runtime','mcp_transport',
    'authorization','read_only','minimum_validation','minimum_release',
)
FULL_COUNT=351
ATOMIC_COUNT=332
GATE_COUNT=19


def fail_if(condition: bool, message: str) -> None:
    if condition:
        raise AssertionError(message)


def jsonl(filename: Path) -> list[dict]:
    return [json.loads(line) for line in filename.read_text(encoding='utf-8').splitlines() if line.strip()]


def phase_closure(profile_name: str, profiles: dict[str,dict], stack: tuple[str,...]=()) -> set[str]:
    fail_if(profile_name not in profiles, f'unknown release profile: {profile_name}')
    fail_if(profile_name in stack, f'profile inheritance cycle: {profile_name}')
    current=profiles[profile_name]
    all_phases=set(current['gates'])
    if current.get('extends') is not None:
        all_phases |= phase_closure(current['extends'],profiles,(*stack,profile_name))
    return all_phases


def validate_records(original: list[dict], revised: list[dict], events: list[dict],
                     sources: set[str], profiles: dict[str,dict],root: Path) -> dict:
    fail_if(len(original)!=FULL_COUNT or len(revised)!=FULL_COUNT,'expected exact 351 tasks')
    ids=[x['task_id'] for x in revised]
    original_ids=[x['task_id'] for x in original]
    fail_if(ids!=original_ids, 'IDs or original ordering changed')
    fail_if(len(set(ids))!=FULL_COUNT,'duplicate IDs')
    byid={x['task_id']:x for x in revised}
    idsset=set(ids)
    done_count=0
    passed_count=0
    edges=defaultdict(list)
    pending=Counter()
    all_gates={x['phase'] for x in revised if x['task_id'].startswith('gate.')}
    fail_if(len(all_gates)!=GATE_COUNT,'not exactly 19 phase gates')
    fail_if(set(profiles['minimum_local_read_only']['gates'])!=set(BASE_PHASES),
            'minimal profile gates must be exactly nine Python phases')
    fail_if(profiles['minimum_local_read_only'].get('extends') is not None,
            'minimum base profile must not inherit optional work')
    for profile_name in profiles:
        closure=phase_closure(profile_name,profiles)
        fail_if(not closure <= all_gates, f'unknown phase in {profile_name}')
        if profile_name!='minimum_local_read_only':
            fail_if(not set(BASE_PHASES)<=closure,
                    f'optional profile skipped baseline: {profile_name}')
    fail_if('formal_verification' in set(BASE_PHASES),'formal proof in minimal release')
    fail_if('formal_verification' not in phase_closure('with_formal_verified_rust',profiles),
            'optional formal proof profile missing')
    # Every selected profile must contain the dependencies of each of its
    # selected task phases, not merely inherit the nominal 9 baseline gates.
    for profile_name in profiles:
        selected=phase_closure(profile_name,profiles)
        for record in revised:
            if record['phase'] in selected:
                for dependency in record['depends_on']:
                    if dependency.startswith('gate.'):
                        dependency_phase=dependency.split('.')[1]
                        fail_if(dependency_phase not in selected,
                                f'profile {profile_name} omits required gate {dependency_phase}')
    expected_gate_ids={'gate.'+phase+'.completed' for phase in all_gates}
    fail_if(sum(x['task_id'].startswith('task.') for x in revised)!=ATOMIC_COUNT,'wrong atomic count')
    fail_if(sum(x['task_id'].startswith('gate.') for x in revised)!=GATE_COUNT,'wrong gate count')
    for prev,x in zip(original,revised):
        ident=x['task_id']; deps=x['depends_on']
        fail_if(ident not in idsset or not isinstance(deps,list) or len(deps)!=len(set(deps)),f'invalid deps: {ident}')
        fail_if(not set(deps) <= idsset or ident in deps,f'unknown/circular self-deps: {ident}')
        fail_if(x['plan_version']!='2.0.0-python-first',f'wrong plan version: {ident}')
        should_min=x['phase'] in BASE_PHASES
        fail_if(x['required_for_minimum_release'] != should_min, f'wrong minimum flag: {ident}')
        stage=('python_minimum' if should_min else
               'optional_formal_rust' if x['phase']=='formal_verification' else 'post_minimum')
        fail_if(x['implementation_stage']!=stage, f'wrong implementation stage: {ident}')
        fail_if(len(x['acceptance_criteria'])<8,f'no acceptance criteria: {ident}')
        fail_if(not set(x['source_ids'])<=sources,f'unknown sources: {ident}')
        if x['status']=='done':
            done_count+=1
            fail_if(x['verification_status'] not in ('passed','not_applicable'), f'done without evidence: {ident}')
            fail_if(any(byid[d]['status']!='done' for d in deps),f'done without dependencies: {ident}')
        if x['verification_status']=='passed':
            passed_count+=1
            fail_if(not x['evidence_paths'],f'passed with missing evidence paths: {ident}')
            for rel in x['evidence_paths']:
                p=(root/rel).resolve()
                fail_if(not p.is_relative_to(root.resolve()) or not p.is_file(),f'unsafe/missing evidence: {ident}: {rel}')
        if prev['verification_status']=='passed' and (prev['acceptance_criteria']!=x['acceptance_criteria'] or prev['depends_on']!=deps):
            fail_if(x['verification_status']=='passed' or x['status']=='done',f'stale evidence re-used: {ident}')
        # Prevent false positive from a converted Rust acceptance still implying Rust tools.
        if x['phase'] in ('runtime','minimum_validation','mcp_transport') and x['task_id'].startswith('task.'):
            fail_if('Kani' in x['title'] or 'Rustのビルド' in x['title'],f'Rust prereq in Python phase: {ident}')
        if should_min:
            fail_if('gate.formal_verification.completed' in deps,f'formal gate blocks minimal release: {ident}')
        for dep in deps: edges[dep].append(ident)
        pending[ident]=len(deps)
    for phase in all_gates:
        expected={x['task_id'] for x in revised if x['phase']==phase and x['task_id'].startswith('task.')}
        gate=byid['gate.'+phase+'.completed']
        fail_if(set(gate['depends_on'])!=expected, f'gate dep mismatch: {phase}')
    # If Rust migration tasks are not required, they cannot block the 9-gate minimum.
    all_min={x['task_id'] for x in revised if x['required_for_minimum_release']}
    for x in revised:
        if x['task_id'] in all_min:
            fail_if(any(d not in all_min for d in x['depends_on']),
                    f'baseline task depends on optional phase: {x["task_id"]}')
    queue=deque(sorted(k for k,v in pending.items() if v==0))
    seen=[]
    while queue:
        current=queue.popleft();seen.append(current)
        for child in edges[current]:
            pending[child]-=1
            if pending[child]==0:queue.append(child)
    fail_if(len(seen)!=FULL_COUNT,'dependency cycle detected')
    # Progress is a separate append-only layer over the historical design migration.
    progress=jsonl(root/'progress_events.jsonl') if (root/'progress_events.jsonl').exists() else []
    progress_by_id={event['task_id']:event for event in progress}
    fail_if(len(progress_by_id)!=len(progress),'duplicate progress events')
    fail_if(not set(progress_by_id).issubset(idsset),'progress event has unknown task')
    # Scope of historical migrations must be exactly what was changed in source plan.
    events_by_id={item['task_id']:item for item in events}
    fail_if(len(events_by_id)!=len(events),'duplicate migration events')
    for prev,x in zip(original,revised):
        changed=any(prev.get(k)!=x.get(k) for k in
                    ('title','phase_title','acceptance_criteria','depends_on','required_for_minimum_release','implementation_language','source_ids'))
        fail_if(changed!=(x['task_id'] in events_by_id),f'migration event mismatch: {x["task_id"]}')
        migration=events_by_id.get(x['task_id'])
        if changed:
            fail_if(migration['previous_status']!=prev['status'],
                    f'migration status mismatch: {x["task_id"]}')
        baseline_status=migration['new_status'] if migration else prev['status']
        baseline_verification=('not_executed' if migration and 'verification_reset' in migration['changes']
                               else prev['verification_status'])
        progress_event=progress_by_id.get(x['task_id'])
        if progress_event:
            fail_if(progress_event['from_status']!=baseline_status or
                    progress_event['from_verification']!=baseline_verification,
                    f'progress from-state mismatch: {x["task_id"]}')
            fail_if(x['status']!=progress_event['to_status'] or
                    x['verification_status']!=progress_event['to_verification'] or
                    x['evidence_paths']!=progress_event['evidence_paths'],
                    f'progress to-state mismatch: {x["task_id"]}')
        else:
            fail_if(x['status']!=baseline_status,
                    f'unlogged state change: {x["task_id"]}')
    return {
      'records':FULL_COUNT, 'atomic':ATOMIC_COUNT,'gates':GATE_COUNT,
      'changed_records':len(events), 'new_minimum_atomic':sum(x['task_id'].startswith('task.') and x['required_for_minimum_release'] for x in revised),
      'new_minimum_gates':len(BASE_PHASES),'new_minimum_total':len(all_min),
      'done':done_count,'passed':passed_count,'progress_events':len(progress),'topological_nodes':len(seen),
      'ready_unfinished_tasks':sum(x['task_id'].startswith('task.') and x['status']!='done' and all(byid[d]['status']=='done' for d in x['depends_on']) for x in revised),
      'release_ready':all(byid['gate.'+phase+'.completed']['status']=='done' for phase in BASE_PHASES),
      'profiles':{name:sorted(phase_closure(name,profiles)) for name in profiles},
      'status':'plan_structure_verified; runtime_not_verified',
    }


def run(root: Path) -> dict:
    original=jsonl(root/'history'/'prior_tasks.jsonl')
    revised=jsonl(root/'tasks.jsonl')
    events=jsonl(root/'migration_events.jsonl')
    sources={item['source_id'] for item in jsonl(root/'sources.jsonl')}
    profiles=json.loads((root/'RELEASE_PROFILES.json').read_text())
    return validate_records(original,revised,events,sources,profiles,root)


if __name__=='__main__':
    result=run(Path(__file__).resolve().parents[1]);print(json.dumps(result,ensure_ascii=False,indent=2))
