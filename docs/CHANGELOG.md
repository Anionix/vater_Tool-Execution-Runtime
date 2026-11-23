# 設計の変更履歴

## 2026-11-23 — 2.0.0-python-first

旧計画：Rustの実行骨格 → Rustの公式MCP開発キット → Rustの純粋関数 → Lean/Kani → 初期公開。

修正計画：Pythonの純粋関数 → 権限・入力の独立検証 → **公式Python MCP開発キットで実接続** → 初期公開 → 必要に応じてRust/Lean/Kaniの任意検証。

- 元の351個の作業識別子は変更しない。
- 初期必須を **179作業＋10ゲート** から **159作業＋9ゲート** へ整理。
- `task.runtime.*`（18作業）をPython構築・Python検査へ変更。
- `task.mcp_transport.*`（20作業）を公式Python開発キットの実接続条件へ変更。
- `task.read_only.005` と `task.authorization.021` のRust/Kani必須条件をPython中核へ変更。
- `task.minimum_validation.*`（15作業）をPythonの実試験に変更し、Rust・Lean・Kaniを前提にしない。
- `task.formal_verification.*`（20作業）を任意の後続段階へ移動。個々のRust/Kani/Lean作業は削除しない。
- 変更操作・外部モデル・HTTP・ブラウザ・WebAssembly・WebGPU・その他の規格は任意プロファイルとし、初期ゲートの合格を必須で継承。
- 承認や外部送信を省略して安全宣言するわけではない。外部変更版を公開するときは別のセキュリティ・試験ゲートが必要。
- 条件または証拠の参照が変わった40件以上の以前の合格記録を無効化。実際の実行結果が揃うまで、機械台帳上の完了数を増やさない。

作業ごとの正確な旧値・新値・状態遷移は `migration_events.jsonl` に **203件**保存してある。

### データと状態の契約

- `plan_version`：変更後の設計版。
- `implementation_stage`：`python_minimum` / `post_minimum` / `optional_formal_rust`。
- `verification_status`：検証未実行の変更作業は `not_executed`。旧証拠は消去せず `history` に保存。
- `status=done`：要求する全依存関係も `done`、かつこの条件の証拠がある場合だけ許可。
- `gate.<phase>.completed`：段階のすべての作業を検証したあとに完了。
- `RELEASE_PROFILES.json`：任意拡張は `extends=minimum_local_read_only` によって9ゲートすべてを継承。

### 非保証

設計表を更新しただけで、公式MCP接続、本人認証、実際の外部副作用、Rustプログラム、Lean/Kaniの形式証明が実行されたわけではない。
