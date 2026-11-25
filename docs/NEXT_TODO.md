# 次の作業

契約段階の完了済み項目は `python scripts/verify_phase.py contracts --completed-only` で再検証できる。対象データの版（`task.contracts.007`）は、保存済みの版同士を比較できる。実行直前の対象の最新版を取得して照合する経路の証拠は未確認。

- 正規化の基礎仕様は [RFC 8259](https://www.rfc-editor.org/rfc/rfc8259.html)。キー順、空白、ダイジェスト計算は Vater のローカル契約として試験する。
- 入力形式は [JSON Schema Draft 2020-12](https://json-schema.org/draft/2020-12) の検証器で試験する。

「目的と境界」と「脅威・信頼境界」の完了判定を通過した。次の段階は「契約とデータの意味」。各項目の合格条件と前提は [作業台帳](../backlog/tasks.jsonl) が正本。

## 進捗と検証

- 全体の正式完了数は `python backlog/validate_tasks.py` で集計する。
- 第2段階の証拠に対応する試験を再実行するには、固定済みのPython 3.13環境で `python scripts/verify_phase.py trust` を実行する。複数の項目が同じ試験を使う場合、実行は一度で済む。
- 同じ作業ツリーと環境で全試験を既に実行した場合は、その結果を `python scripts/verify_phase.py trust --report /path/to/results.xml` に渡す。保存時点のファイルと報告書の検査値は `python scripts/verify_phase.py trust --snapshot` で照合できる。

[第1段階の証拠](../evidence/gates/scope.json)・[第2段階の証拠](../evidence/gates/trust.json)
