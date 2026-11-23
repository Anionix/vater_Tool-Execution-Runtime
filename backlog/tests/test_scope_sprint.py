"""Test actual documents, policy, calculation and transport boundary, without the SDK.

TRACE: scope.001 .. scope.018; status done only for executed assertions.
The tests DO NOT demonstrate official SDK interoperability.
"""
from __future__ import annotations
import asyncio
import importlib.util
import json
import hashlib
from pathlib import Path
import sys
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "application"
sys.path.insert(0, str(APP / "src"))
sys.path.insert(0, str(APP / "scripts"))
from verified_execution.core import CalculationService, LocalAuthority, checked_add
from verified_execution.request_limits import ReadOnlyPolicy, BoundedReadOnlyExecutor
from guarded_stdio import validate_json_line, WireInputRejected
from launch_guarded_mcp import construct_command

POLICY = json.loads((APP / "config" / "limits.json").read_text(encoding="utf-8"))
TASKS = [json.loads(line) for line in (ROOT / "tasks.jsonl").read_text().splitlines() if line]

class ScopeSprintTests(unittest.TestCase):
    def test_scope_001_identical_single_purpose(self):
        goal = 'Pythonの純粋な計算を権限と入力境界の内側で安全に実行し、公式MCPのローカル標準入出力から呼び出せるようにする。'
        for p in (ROOT / "README.md", ROOT / "ARCHITECTURE.md", ROOT / "SCOPE_CONTRACT.md"):
            self.assertIn(goal, p.read_text())
        self.assertEqual((ROOT / "README.md").read_text().count('**目的：'+goal+'**'), 1)

    def test_scope_003_python_first_venv_and_pure_calculation(self):
        doc=(ROOT / 'SCOPE_CONTRACT.md').read_text()
        pyproject=(APP / 'pyproject.toml').read_text()
        self.assertIn('python3.13 -m venv .venv',doc)
        self.assertIn('requires-python = ">=3.13"',pyproject)
        self.assertIn('Rustコンパイル',doc)
        self.assertEqual(checked_add(2,3),5)

    def test_scope_004_separated_mcp_and_pure_test_suites(self):
        # Exact source checks; SDK tests are explicitly skipped if not installed.
        core=(APP/'src/verified_execution/core.py').read_text()
        server=(APP/'server_mcp.py').read_text()
        sdk=(APP/'tests/test_official_mcp_sdk.py').read_text()
        self.assertNotIn('from mcp',core)
        self.assertIn('from mcp.server import Server',server)
        self.assertIn('pytest.mark.skipif',sdk)
        self.assertIn('def test_sdk_stdio_modern',sdk)
        self.assertEqual(CalculationService(LocalAuthority.calculator_only()).execute('checked_add',{'left':1,'right':2}).result,3)

    def test_scope_006_two_output_semantic_variants(self):
        contract=json.loads((APP/'contracts/checked_add.output.contract.json').read_text())
        self.assertEqual(set(contract['domains']), {'success','failure'})
        service=CalculationService(LocalAuthority.calculator_only())
        good=service.execute('checked_add', {'left':1,'right':2})
        bad=service.execute('checked_add', {'left':1,'right':'2'})
        self.assertEqual(good.result,3)
        self.assertIsNone(good.error_code)
        self.assertIsNone(bad.result)
        self.assertIn(bad.error_code, contract['domains']['failure']['error_code']['enum'])
        self.assertIs(contract['domains']['success']['mcp_is_error'],False)
        self.assertIs(contract['domains']['failure']['mcp_is_error'],True)

    def test_scope_009_non_goals_fully_excluded(self):
        doc=(ROOT/'SCOPE_CONTRACT.md').read_text()
        for word in ('認証付き遠隔公開','決済','未信頼コードの実行','GPU','HTTP','APIキー送信'):
            self.assertIn(word,doc)
        self.assertNotIn('sqlite3', (APP/'src/verified_execution/core.py').read_text().lower())

    def test_scope_010_real_byte_limit_and_bound_launch(self):
        self.assertEqual(POLICY['request_input_max_bytes'],4096)
        cmd=construct_command()
        self.assertEqual(cmd[cmd.index('--max-bytes')+1], '4096')
        self.assertEqual(cmd[cmd.index('--max-depth')+1], str(POLICY['json_max_depth']))
        small=b'{' + b'"a":"' + b'x'*4080 + b'"}' + b'\n'
        self.assertLessEqual(len(small),4096)
        self.assertEqual(validate_json_line(small,max_bytes=4096),small)
        too_large=b'{' + b'"a":"' + b'x'*4089 + b'"}' + b'\n'
        self.assertGreater(len(too_large),4096)
        with self.assertRaises(WireInputRejected):
            validate_json_line(too_large,max_bytes=4096)

    def test_scope_011_request_scoped_deadline_bounded(self):
        import anyio
        self.assertEqual(POLICY['readonly_request_timeout_ms'],2000)
        async def work():
            executor=BoundedReadOnlyExecutor(ReadOnlyPolicy(20,1))
            def sleeping():
                time.sleep(0.12)
                return 'late'
            with self.assertRaises(TimeoutError):
                await executor.run(sleeping)
            self.assertEqual(await executor.run(lambda:42), 42)
        anyio.run(work)
        self.assertIn('await bounded_readonly.run(service.execute', (APP/'server_mcp.py').read_text())

    def test_scope_012_single_concurrency_limit(self):
        import anyio
        self.assertEqual(POLICY['readonly_max_concurrent'],1)
        async def run():
            executor=BoundedReadOnlyExecutor(ReadOnlyPolicy(2000,1))
            lock=threading.Lock()
            active=[0];peak=[0];values=[]
            def task():
                with lock:
                    active[0]+=1;peak[0]=max(peak[0],active[0])
                time.sleep(0.035)
                with lock:active[0]-=1
                return 1
            async def worker():
                values.append(await executor.run(task))
            async with anyio.create_task_group() as tg:
                for _ in range(4):tg.start_soon(worker)
            self.assertEqual(peak[0],1)
            self.assertEqual(values,[1]*4)
        anyio.run(run)
        self.assertIn('同時実行上限を増やせるのは', (ROOT/'SCOPE_CONTRACT.md').read_text())

    def test_scope_014_five_measurable_gate_categories(self):
        doc=(ROOT/'SCOPE_CONTRACT.md').read_text()
        for item in ('**正常**','**異常**','**権限**','**互換**','**再現性**'):
            self.assertIn(item,doc)
        self.assertIn('未実行',doc)

    def test_scope_015_not_locked_yet(self):
        self.assertIn('mcp==2.3.0',(APP/'pyproject.toml').read_text())
        self.assertFalse((APP/'requirements.lock').exists())
        self.assertIn('blocked',(ROOT/'DEPENDENCY_LOCK_STATUS.md').read_text())
        self.assertTrue(any(x['task_id']=='task.scope.015' and x['status']=='blocked' for x in TASKS))

    def test_scope_018_immutable_identifiers_and_progress_events(self):
        old=[json.loads(s) for s in (ROOT/'history/prior_tasks.jsonl').read_text().splitlines() if s]
        self.assertEqual([t['task_id'] for t in old],[t['task_id'] for t in TASKS])
        events=[json.loads(s) for s in (ROOT/'progress_events.jsonl').read_text().splitlines() if s]
        by={t['task_id']:t for t in TASKS}
        self.assertEqual(len(events),11)
        self.assertEqual(len({e['task_id'] for e in events}),11)
        for event in events:
            self.assertEqual(by[event['task_id']]['status'],event['to_status'])
            self.assertIn(event['from_status'],('in_progress','not_started'))
            self.assertTrue(event['reason'])
