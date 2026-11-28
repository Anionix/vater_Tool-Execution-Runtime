# 次の作業

作業台帳（[backlog/tasks.jsonl](../backlog/tasks.jsonl)）の351件は、任意段階を含めてすべて正式完了（`python backlog/validate_tasks.py` で集計）。残っているのは台帳の外の判断と確認で、どれも人が行う。

## 人による確認が必要なもの

- 「目的と境界」の段階の証拠（[evidence/gates/scope.json](../evidence/gates/scope.json)）は、README を人が確認し直したうえで2026-11-28に作り直した。18件の作業の合格条件を現在のファイルに照らす試験（`tests/test_scope_reconciliation.py`）がすべて通り、`python scripts/release_decision.py` は初期公開と拡張公開の両方を許可する。README か範囲の契約（`docs/SCOPE_CONTRACT.md`）が再び変わったら、人が文面を確認してから `python scripts/rebuild_scope_gate.py --rechecked README.md`（確認したファイルごとに指定）で作り直す。確認の指定がなければ作り直しは拒否される。
- 承認の記録（`config/local_tool.json`、`evidence/formal/SHA256SUMS`）は、利用者が明示した場合だけ更新する。
- 外部の課題を作成する実接続の試験は、人が依頼したとき（`run-external-evidence` ラベル）だけ実行する。

## 任意段階の判断

WebAssembly 隔離、追加通信規格、WebMCP、WebGPU は試作と計測まで完了し、いずれもリリースには導入しないと判断した。理由と再検討の条件は [OPTIONAL_PROTOTYPES.md](OPTIONAL_PROTOTYPES.md) にある。条件が変わった場合（外部作成コードの受付、一覧を求める利用者、フラグなしの WebMCP、ハードウェアの GPU）に、その段階の記録を見直す。

## 進捗と検証

- 段階ごとの証拠に対応する試験は `python scripts/verify_phase.py <段階>` で再実行できる。複数の項目が同じ試験を使う場合、実行は一度で済む。
- 保存時点のファイルと報告書の検査値は `python scripts/verify_phase.py <段階> --snapshot` で照合できる。
- 証拠の鮮度は `python scripts/evidence_freshness.py` で一覧でき、段階の証拠は `--refresh-gate <段階>` で試験を再実行して結び直す（失敗した場合は記録を変えない）。
