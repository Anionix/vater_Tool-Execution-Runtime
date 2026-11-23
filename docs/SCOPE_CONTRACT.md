# 初期版の明示的な仕様 — 全11件のうち文書対象10件

## 目的（唯一の正規文）

**Pythonの純粋な計算を権限と入力境界の内側で安全に実行し、公式MCPのローカル標準入出力から呼び出せるようにする。**

## 初期環境

- Python **3.13以上**を使用する。`application/pyproject.toml` の `requires-python` も3.13以上に設定。
- 初期計算は副作用のない `checked_add` のみ。`application/src/verified_execution/core.py` はMCPなしで試験できる。
- Python仮想環境は `python3.13 -m venv .venv` で作る。通常の実行は `source .venv/bin/activate` 後に `python -m pip install -e '.[test]'`。ただしパッケージ取得が必要な場合はネットワーク接続が必要。
- ローカルの依存導入済み環境では `PYTHONPATH=application/src python -m pytest -q application/tests/test_core.py` が実行できる。
- **公式MCP標準入出力**は `application/server_mcp.py`。Pythonの計算試験と、公式開発キットを用いた接続試験を別集計する。
- **通信の本番起動**には公式SDKの取得とその実接続検証が必要。HTTP、WebMCP、他社モデルへの接続は初期版に含めない。

## 収録しないもの（初期公開）

- 認証付き遠隔公開、HTTP、外部モデルへのAPIキー送信、決済、変更操作、未信頼コードの実行、ブラウザ実行、GPU、Rustコンパイル、Lean/Kani形式証明。
- SQLite記録付きツール `checked_add_recorded` は後続段階の別の公開プロファイルに属する。初期版は未設定で公開しない。

## 入力・出力

- 入力は `left`, `right` の正確な2要素。それぞれ0〜4294967295の整数。真偽値、文字列、浮動小数、余分な要素は拒否。
- `application/contracts/checked_add.output.contract.json` に成功 `{result: 3}` と失敗 `{error_code: "INVALID_ARGUMENT"}` の意味を定義。
- Python中核は `ExecutionResult.to_dict()`、公式MCP側は `CallToolResult.is_error` と `TextContent` を使用する。**両者の外側の形は同一ではない**。
- 計算上の失敗は副作用のある実行の結果不明状態と区別する。

## 量・時間・同時実行の上限

- `application/config/limits.json` を唯一の値の定義として扱う。受信1行は最大 **4096バイト**、返信1行65536バイト、JSON階層32段。
- 実際のバイト数検査はSDKより前の `application/scripts/guarded_stdio.py` に実装されている。公式開発キットが未導入なので通信全体の通し試験は未合格。
- 読み取り専用 `checked_add` の要求時間は **2000ミリ秒**、同時実行は **1件**。`application/server_mcp.py` は `BoundedReadOnlyExecutor` を使用。
- 制限時間には待機列の時間を含む。期限切れは `TIMEOUT`。既に起動したPythonスレッドを強制停止しないため、**副作用や未信頼コードの処理には流用禁止**。
- 同時実行上限を増やせるのは、公式実接続・競合試験・負荷試験・メモリ予算・取消し時の動作確認が再合格した場合だけ。変更時は構成値、テスト、履歴を同時更新。

## 合格条件を5種類に分離

1. **正常**：0+0→0、最大値+0→最大値、通常整数の計算が正しい。
2. **異常**：範囲外・余分な引数・不正な型・サイズ超過・期限切れを拒否。
3. **権限**：ホスト設定が拒否すれば実行できない。要求の自己申告を信用しない。
4. **互換**：公式MCP開発キットで新旧の標準入出力接続を別々に通す。**未実行**。
5. **再現性**：同じ入力で同じ出力、テスト実行記録、依存関係の検査値を残す。SDKのロックが未作成なら未完了。

## 作業台帳の変更規則

- 全351件の `task_id` は不変。欠番の再利用・別の意味での識別子の使い回しは禁止。
- 設計の変更は `migration_events.jsonl`、**進捗の変更は `progress_events.jsonl`** に旧状態・新状態・証拠を保存。
- `status=done` に移せるのは、合格条件に沿った実行した試験と証拠がある作業だけ。依存関係が未完了なら完了にしない。
- 証拠を再利用する場合は版と元ファイルの検査値を確認する。失敗・未実行を成功へ書き換えない。

## 既知の未達

- `task.scope.015` 依存の完全固定：`mcp==2.3.0` は直接指定されているが、推移的な依存と各配布ファイルの検査値は未入手。完了扱いしない。
- 公式MCPの通信と仕様への適合性を実行して検証したわけではない。
