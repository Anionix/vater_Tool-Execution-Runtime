# 次の作業 — 元の351件・Python先行版

## 初回11件の結果

- `task.scope.001`, `.003`, `.004`, `.006`, `.009`, `.010`, `.011`, `.012`, `.014`, `.018`：**新規10件完了**。試験と検査値は `evidence/scope_tasks/`。
- `task.scope.015`：**保留**。公式MCP依存の完全な検査値付きロックファイルの生成・導入ができない。

## 次に実施可能な方向

1. ネットワークを利用できるPython3.13環境で `task.scope.015` を実施。すべての推移的依存をハッシュ付き固定し、クリーン環境で導入を再現する。
2. `gate.scope.completed` の全18作業を確認する。**上の依存固定が終わるまで合格にしない**。
3. `task.trust.001` 以降の信頼境界の設計・コード監査は並行して着手できる。ただし正式な `done` は `gate.scope.completed` に依存する。
4. 公式SDKが導入できたら、MCP実接続の14件を再実行し、公開条件の互換試験を進める。

## 自動確認

```bash
python validate_tasks.py
python scripts/validate_redesign.py
python scripts/verify_reconstruction.py
python scripts/verify_scope_evidence.py
python -m unittest discover -s tests -v
```

**正式完了 17 / 351。残り334。初期版168件のうち17件完了・151件残り。**
