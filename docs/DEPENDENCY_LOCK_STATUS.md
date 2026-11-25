# 依存パッケージの固定状況

Python 3.13・Linux向けに33パッケージの版と配布物のSHA-256を固定した。公式開発キットは `mcp==2.3.0`。第1段階の完了判定には固定済みの依存を用いた実接続試験の結果を使った。[固定済みの一覧](../requirements-py313.lock)・[第1段階の証拠](../evidence/gates/scope.json)

第2段階では、クリーン環境への導入時に記録した各配布物の取得元・版・SHA-256を照合する。検証の対象は記録された環境と配布物。[配布物の記録](../evidence/verified_tasks/task.trust.018.json)・[再生成手順](DEPENDENCY_LOCK_WORKFLOW.md)
