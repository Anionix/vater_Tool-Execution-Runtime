"""Rebuild human-facing reports from the versioned 351-row JSONL task ledger.

STATE: source ledger -> validated dependencies -> markdown render -> report artifact.
Do not add task completion through this reporting path.
"""
from __future__ import annotations
from collections import Counter, defaultdict
import json
from pathlib import Path
from validate_redesign import run, jsonl, BASE_PHASES

ROOT=Path(__file__).resolve().parents[1]
items=jsonl(ROOT/'tasks.jsonl')
old=jsonl(ROOT/'history'/'prior_tasks.jsonl')
changes=jsonl(ROOT/'migration_events.jsonl')
summary=run(ROOT)
by={x['task_id']:x for x in items}

labels={'not_started':'未着手','in_progress':'実施中または要再検証','review':'個別証拠あり・前提待ち','done':'正式完了','blocked':'ブロック'}
lines=['# Python先行版 — 元の351件の実装作業一覧','','- 元の識別子を変えず、Python版の条件に置き換えた台帳。',f'- [x] が付くのは、前提条件と合格証拠がそろった{summary["done"]}件のみ。','- 実装機能と作業の合格は別。契約を変更した項目の古い合格証拠は無効。','']
order=[*BASE_PHASES,*[p for p in dict.fromkeys(x['phase'] for x in items) if p not in BASE_PHASES]]
for phase in order:
 entries=[x for x in items if x['phase']==phase]
 lines.extend(['## '+entries[0]['phase_title'],f'作業 {len(entries)-1}件・完了判定 1件 / 初期必須：{"はい" if phase in BASE_PHASES else "いいえ"}',''])
 for item in entries:
  cb='x' if item['status']=='done' else ' '
  lines.append(f'- [{cb}] `{item["task_id"]}` {item["title"]} — {labels.get(item["status"],item["status"])}')
  lines.append('  - 合格：'+item['acceptance_criteria'])
 lines.append('')
(ROOT/'TASKS.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
summary2={**summary,
 'prior_minimum_total':sum(x['required_for_minimum_release'] for x in old),
 'prior_minimum_atomic':sum(x['required_for_minimum_release'] and x['task_id'].startswith('task.') for x in old),
 'prior_minimum_gates':sum(x['required_for_minimum_release'] and x['task_id'].startswith('gate.') for x in old),
 'changed_acceptance':sum('acceptance_criteria' in x['changes'] for x in changes),
 'prior_passed':sum(x['verification_status']=='passed' for x in old),
 'retained_passed':sum(x['verification_status']=='passed' and x['task_id'] not in {e['task_id'] for e in jsonl(ROOT/'progress_events.jsonl')} for x in items),
 'new_verified':sum(x['verification_status']=='passed' and x['task_id'] in {e['task_id'] for e in jsonl(ROOT/'progress_events.jsonl')} for x in items),
 'expired_prior_verifications':sum(x['verification_status']=='passed' for x in old)-sum(x['verification_status']=='passed' and x['task_id'] not in {e['task_id'] for e in jsonl(ROOT/'progress_events.jsonl')} for x in items),
}
(ROOT/'VALIDATION_SUMMARY.json').write_text(json.dumps(summary2,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
phase_rows=[]
for phase in order:
 arr=[x for x in items if x['phase']==phase]
 phase_rows.append((arr[0]['phase_title'],len(arr)-1,sum(x['status']=='done' for x in arr),sum(x['verification_status']=='passed' for x in arr), '必須' if phase in BASE_PHASES else '後続'))
report=['# Python先行設計への変更報告','','## 集計','','| 指標 | 旧設計 | 修正後 |','|---|---:|---:|',
 f'| 全レコード | 351 | {len(items)} |',
 f'| 個別作業 | 332 | {summary["atomic"]} |',
 f'| 完了判定 | 19 | {summary["gates"]} |',
 f'| 初期版に必須の作業 | {summary2["prior_minimum_atomic"]} | {summary["new_minimum_atomic"]} |',
 f'| 初期版に必須の完了判定 | {summary2["prior_minimum_gates"]} | {summary["new_minimum_gates"]} |',
 f'| **初期版の必須合計** | **{summary2["prior_minimum_total"]}** | **{summary["new_minimum_total"]}** |',
 f'| 正式完了 | 7 | {summary["done"]} |',
 f'| 古い個別合格証拠が引き続き有効 | 60 | {summary2["retained_passed"]} |',
 f'| 新規検証で合格した作業 | 0 | {summary2["new_verified"]} |',
'',
 f'**変更された作業記録：{summary["changed_records"]}件**。そのうち合格条件の文面変更：{summary2["changed_acceptance"]}件。旧検証の失効：{summary2["expired_prior_verifications"]}件。',
 '旧証拠の失効は保守的に扱う。公式MCP実接続は未確認。新たなscopeの個別試験はevidence/scope_sprint.xmlを参照。','',
 '## 段階別','','| 段階 | 個別作業 | 正式完了 | 個別証拠保持 | 初期版 |','|---|---:|---:|---:|---|']
for row in phase_rows:report.append('| '+' | '.join(map(str,row))+' |')
report.extend(['', '## 現時点で開始できる残りの作業','','前提条件がすでに完了している作業（未着手とは限らない）。',''])
ready=[x for x in items if x['task_id'].startswith('task.') and x['status']!='done' and all(by[d]['status']=='done' for d in x['depends_on'])]
for x in ready:report.append(f'- `{x["task_id"]}` {x["title"]} — 合格：{x["acceptance_criteria"]}')
report.extend(['', '## 絶対に未完了として残す条件','',
 '- 公式MCP Python開発キットの実インストール、2026-07-28/旧版実接続、公式適合性検査。',
 '- 遠隔の本人確認・人間の承認・実在する外部サービスの副作用と結果照会。',
 '- Rustへの移植、LeanとKaniの形式検証、独立した第三者監査。',
 '- この設計変更は試験実行の代わりではなく、**公開判定はBLOCKED**。',
 '', '## ファイルの意味','',
 '- `tasks.jsonl`：新しい機械用台帳（全351件）。',
 '- `migration_events.jsonl`：変更した203件の旧値と新値。',
 '- `history/prior_tasks.jsonl`：以前の台帳の完全な複製。',
 '- `RELEASE_PROFILES.json`：初期版と任意拡張の依存関係。',
 '- `TASKS.md`：人間向けの全件チェックリスト。',
 '- `scripts/validate_redesign.py`：依存関係、証拠の再利用、公開ゲートを検証。',
 '- `tests/test_plan_migration.py`：意図的な構造破壊を検出する独立試験。',
 ])
(ROOT/'REPORT.md').write_text('\n'.join(report)+'\n',encoding='utf-8')
print('Report written; ready tasks=',len(ready))
