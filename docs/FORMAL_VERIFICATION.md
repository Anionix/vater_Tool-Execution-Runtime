# Vater の形式検証仕様

対象は Python のローカル加算と、単一ホストの台帳による実行許可。以下は証明すべき命題と実装の対応表であり、Python 実装との一致は既存テストで観測する。数学的な命題の証明と、稼働中の実装が命題の前提を満たすことは別々に判定する。

| 追跡識別子 | Python の実行経路 | 形式モデルの対象 | Rust 移植候補 |
| --- | --- | --- | --- |
| `task.formal_verification.001` | `core.checked_add` | 32 ビット符号なし整数の範囲、数学的な和、範囲外の失敗 | 副作用のない加算だけを候補とする |
| `task.formal_verification.002` | `ledger.OperationLedger.begin_dispatch` | 承認の要否と承認済み実行 | 永続化を伴わない承認条件の述語 |
| `task.formal_verification.003` | `core.LocalAuthority.permits` と `ledger.OperationLedger._check_dispatch_grant` | 現在の権限を使った許可判定 | 時計と取消しを入力にした判定 |
| `task.formal_verification.004` | `ledger.OperationLedger.begin_dispatch` | 承認の束縛と差し替えの拒否 | 束縛情報の比較 |
| `task.formal_verification.005` | `core.ExecutionTrace.move` | 同期計算の状態遷移 | 状態と遷移許可の純粋な判定 |

`task.formal_verification.019` の実際の呼出し経路は、標準入出力の `server_mcp.build_server` から読み取り専用の `runtime.ToolRuntime.execute`（旧名 `CalculationService.execute`）へ進み、`tools.ToolSpec` を経て `core.checked_add`・`core.LocalAuthority.permits`・`core.ExecutionTrace.move` を呼ぶ経路と、記録付きの `RecordedCalculator.execute` から台帳へ進む経路。ワーカー付きの入口は `CancellableLeasedToolBridge.acall` と `LeasedLedger.start` を使うため、台帳の承認に関する下記の命題は `OperationLedger.begin_dispatch` の範囲に限る。

## 検証する命題

- `task.formal_verification.002`。準備済みかつ中断されていない操作を対象とする。承認が必要で、対応する未使用かつ有効期限内の承認が存在しないなら、書き込みトランザクションの終了後も操作は送信済みに進まない。
- `task.formal_verification.003`。信頼するホストが権限を渡し、判定に使う時計が正しく、判定時点の権限状態が取得できると仮定する。発行元、操作、送信先のいずれかが許可条件に合わないか、権限が期限切れまたは取消済みなら、権限判定は拒否を返す。台帳の内部呼出しで権限を省略する経路や、判定後の外部通信はこの命題に含めない。
- `task.formal_verification.004`。主体、ツール、対象、対象版、正規化した引数の検査値を承認と予約に束縛する。承認と予約で対象版または引数の検査値が異なれば承認は消費されず、実行へ進まない。異なる正規化済み引数から同じ検査値が生じないことは暗号学的な仮定。
- `task.formal_verification.005`。読み取り専用計算の状態集合は `received`、`validated`、`authorized`、`running`、`completed`、`rejected`、`failed`。許す遷移は、受領から検証済みまたは拒否、検証済みから許可済みまたは拒否、許可済みから実行中、実行中から完了または失敗の7本。残り42本は禁止する。具体例は、検証を飛ばした実行、いずれの状態からも自分自身への遷移、完了・拒否・失敗からの再開。

## 証拠と対象外

`task.formal_verification.014`。この版では Rust 移植を採用していないため Kani の対象はゼロ。候補は対応表に留める。将来採用した場合は、移植関数の入力範囲、境界値、時計と権限の仮定、除外された入力を関数単位で記録する。

`task.formal_verification.017`。SQLite の並行予約、承認消費、非同期取消し、強制終了からの復元は実行テスト済みの範囲で扱う。Python の並行処理に対する Kani の証明はない。

`task.formal_verification.020`。Python 実行系、コンパイラ、SQLite の永続性とロック、公式開発キット、外部サービスの動作、時計の整合、暗号学的検査値は現時点で未証明。対象外の前提を含む運用全体の安全性を主張しない。

## Lean で検査した範囲

`task.formal_verification.006` と `task.formal_verification.007`。仕様は [VaterLocal.lean](../formal/VaterLocal.lean)。Python で整数型と符号を検証した後の値を Lean の自然数に対応させ、Python の加算成功を `some`、上限超過を `none` に対応させる。加算の正しさ・成功条件・上限超過、および承認や権限がない場合のモデル上の実行拒否を Lean 4.19.0 で検査した。台帳そのものを形式化した証明は含まない。

`task.formal_verification.008` と `task.formal_verification.009`。5定理の公理一覧は [検査記録](../evidence/formal/lean-4.19.0.log) にある。未証明の穴に相当する `sorryAx` は一覧にない。加算の成功条件と上限超過の定理には `propext` と `Quot.sound` が現れる。

`task.formal_verification.016`。ゼロと最大値の和、最大値に1を足す場合、および入力が最大値を超える場合を Lean の例と Python の境界テストで照合する。Rust 移植を採用した場合は同じ入力で再比較する。

`task.formal_verification.018`。ルートで `sha256sum -c evidence/formal/SHA256SUMS` を実行すると、対象ソースか結果が変わった場合に失敗する。再検証は `lean --trust=0 formal/VaterLocal.lean`。これは Lean 自身の検査であり、独立した別の処理系による検査は未実施。
