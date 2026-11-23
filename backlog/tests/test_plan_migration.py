"""Negative and positive deterministic checks for the 351-task Python-first plan."""
from __future__ import annotations
import copy, json, sys, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from validate_redesign import run, validate_records, jsonl, phase_closure


class MigrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original=jsonl(ROOT/'history'/'prior_tasks.jsonl')
        cls.current=jsonl(ROOT/'tasks.jsonl')
        cls.events=jsonl(ROOT/'migration_events.jsonl')
        cls.sources={x['source_id'] for x in jsonl(ROOT/'sources.jsonl')}
        cls.profiles=json.loads((ROOT/'RELEASE_PROFILES.json').read_text())

    def check_corrupted(self, action):
        new=copy.deepcopy(self.current)
        profiles=copy.deepcopy(self.profiles)
        migrations=copy.deepcopy(self.events)
        action(new,profiles,migrations)
        with self.assertRaises(AssertionError):
            validate_records(self.original,new,migrations,self.sources,profiles,ROOT)

    def test_actual_redesign_passes_independent_validator(self):
        self.assertEqual(run(ROOT)['records'],351)

    def test_unchanged_original_identifiers(self):
        self.assertEqual([x['task_id'] for x in self.original],[x['task_id'] for x in self.current])

    def test_minimum_has_nine_gates(self):
        x=run(ROOT)
        self.assertEqual((x['new_minimum_atomic'],x['new_minimum_gates']), (159,9))

    def test_python_baseline_does_not_inherit_formal_proofs(self):
        self.assertNotIn('formal_verification',phase_closure('minimum_local_read_only',self.profiles))
        self.assertIn('formal_verification',phase_closure('with_formal_verified_rust',self.profiles))

    def test_gate_not_ready(self):
        self.assertFalse(run(ROOT)['release_ready'])

    def test_duplicate_id_rejected(self):
        self.check_corrupted(lambda tasks,profiles,logs: tasks[1].update(task_id=tasks[0]['task_id']))

    def test_missing_gate_rejected(self):
        def mutate(tasks,profiles,logs):
            for item in tasks:
                if item['task_id']=='gate.scope.completed':item['depends_on']=item['depends_on'][1:]
        self.check_corrupted(mutate)

    def test_formal_proof_in_initial_gate_rejected(self):
        def mutate(tasks,profiles,logs):
            next(x for x in tasks if x['task_id']=='task.minimum_validation.001')['depends_on'].append('gate.formal_verification.completed')
        self.check_corrupted(mutate)

    def test_formal_proof_in_minimum_profile_rejected(self):
        self.check_corrupted(lambda tasks,profiles,logs: profiles['minimum_local_read_only']['gates'].append('formal_verification'))

    def test_profile_missing_baseline_rejected(self):
        self.check_corrupted(lambda tasks,profiles,logs: profiles['with_http'].update(extends=None))

    def test_optional_profile_missing_secondary_prerequisite_rejected(self):
        self.check_corrupted(lambda tasks,profiles,logs: profiles['with_extra_protocols'].update(gates=['optional_more_protocols']))

    def test_profile_inheritance_cycle_rejected(self):
        self.check_corrupted(lambda tasks,profiles,logs: profiles['minimum_local_read_only'].update(extends='with_http'))

    def test_changed_pass_reuse_rejected(self):
        def mutate(tasks,profiles,logs):
            t=next(x for x in tasks if x['task_id']=='task.runtime.017')
            t['verification_status']='passed';t['status']='review';t['evidence_paths']=['evidence/tasks/task.scope.005.json']
        self.check_corrupted(mutate)

    def test_missing_migration_event_rejected(self):
        self.check_corrupted(lambda tasks,profiles,logs: logs.pop())

    def test_optional_dependency_inside_baseline_rejected(self):
        def mutate(tasks,profiles,logs):
            next(x for x in tasks if x['task_id']=='task.scope.004')['depends_on'].append('gate.provider_adapters.completed')
        self.check_corrupted(mutate)

    def test_unknown_source_rejected(self):
        self.check_corrupted(lambda tasks,profiles,logs: tasks[0]['source_ids'].append('nonexistent-source'))

    def test_missing_evidence_file_rejected(self):
        def mutate(tasks,profiles,logs):
            next(x for x in tasks if x['status']=='done')['evidence_paths']=['/etc/hosts']
        self.check_corrupted(mutate)

    def test_all_gates_remain_blocked(self):
        self.assertEqual(sum(x['task_id'].startswith('gate.') and x['status']=='done' for x in self.current),0)

    def test_original_seven_done_retained(self):
        old_done={x['task_id'] for x in self.original if x['status']=='done'}
        now_done={x['task_id'] for x in self.current if x['status']=='done'}
        self.assertEqual(len(old_done),7)
        self.assertLessEqual(old_done,now_done)


if __name__=='__main__':unittest.main()
