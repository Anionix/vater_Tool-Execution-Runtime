"""Negative tests for forged/local proof claims; no SDK dependency."""
import copy
import json
import sys
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from verify_scope_evidence import run, verify_one, EvidenceError

class EvidenceIntegrityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.task=next(json.loads(s) for s in (ROOT/'tasks.jsonl').read_text().splitlines()
                      if 'task.scope.010' in s)
        cls.record=json.loads((ROOT/'evidence/scope_tasks/task.scope.010.json').read_text())
        cls.test_names={cls.record['test_name']}

    def test_full_scope_evidence_passes(self):
        self.assertEqual(run()['newly_done'],10)

    def test_forged_source_checksum_rejected(self):
        r=copy.deepcopy(self.record)
        name=next(iter(r['source_sha256']))
        r['source_sha256'][name]='f'*64
        with self.assertRaises(EvidenceError):
            verify_one(self.task,r,self.test_names)

    def test_forged_test_name_rejected(self):
        r=copy.deepcopy(self.record);r['test_name']='test_scope_010_not_executed'
        with self.assertRaises(EvidenceError):verify_one(self.task,r,self.test_names)

    def test_unexecuted_result_rejected(self):
        r=copy.deepcopy(self.record);r['executed']=False
        with self.assertRaises(EvidenceError):verify_one(self.task,r,self.test_names)

    def test_cross_task_reuse_rejected(self):
        r=copy.deepcopy(self.record);r['task_id']='task.scope.009'
        with self.assertRaises(EvidenceError):verify_one(self.task,r,self.test_names)

    def test_outside_path_rejected(self):
        r=copy.deepcopy(self.record);r['source_sha256']={'/etc/hosts':'a'*64}
        with self.assertRaises(EvidenceError):verify_one(self.task,r,self.test_names)
