# 追加通信規格の判断と試作（任意段階 `optional_more_protocols`）

対象は GraphQL（001〜003）、gRPC と Protocol Buffers（004、005）、Agent2Agent（006、007）、エディター接続（008）。Webhook 受信（009〜011）と共通中核を変えない条件（012）は完了済み。試作は `src/vater_optional/` にあり、リリースのパッケージにもロックにも入らない。graphql-core 3.3.2 は任意試作用のロック [requirements-optional-py313.lock](../../requirements-optional-py313.lock) にだけ入る。

## GraphQL への利用要求（task.optional_more_protocols.001）

実在するローカル HTTP 入口（`verified_execution.http_local`）と GraphQL 試作を同じ端末の実接続で動かし、`checked_add` の仕事20件について、3種類の問い合わせに必要な要求数と応答本文のバイト数を数えた（[graphql-demand.json](../../evidence/optional/graphql-demand.json)、`scripts/measure_graphql_demand.py`）。

| 問い合わせ | REST の要求数 | REST の応答 | GraphQL の要求数 | GraphQL の応答 |
| --- | --- | --- | --- | --- |
| 識別子が分かっている20件の状態と結果 | 20 | 911 バイト | 1 | 1,490 バイト |
| 同じ20件と、各ツールの入力スキーマ | 21（OpenAPI 文書全体を含む） | 7,527 バイト | 1 | 2,707 バイト |
| 識別子を知らない成功済みの仕事の一覧 | 不可（一覧の入口がない） | — | 1 | 1,090 バイト |

REST で不足するのは、一覧（状態で絞り込んだ仕事の列挙）と、複数の仕事やツール情報を1回で取る問い合わせ。最初の問い合わせでは往復数が20から1に減る一方、GraphQL の応答は識別子を含む分だけ大きい。スキーマを伴う問い合わせでは OpenAPI 文書全体を取らずに済むため、応答も約3分の1になる。ただし現在の利用者は単一のローカル診断クライアントと MCP クライアントで、仕事の一覧や一括照会を求める画面はない。そのため GraphQL はリリースには導入しない。一覧を求める利用者が現れた場合も、まず REST に一覧の入口を加え、複数資源をまたぐ項目選択が必要になった段階で GraphQL を再検討する。

## GraphQL の問い合わせ深さと計算量の制限（task.optional_more_protocols.002）

[graphql_api.py](../../src/vater_optional/graphql_api.py) は、次の順に検査し、どれかに反すればリゾルバーを1つも実行せずに拒否する。

1. 本文 4 KiB 以下、問い合わせ 400 トークン以下（`parse(max_tokens=...)`）。
2. GraphQL 標準の検証（未知の項目、フラグメントの循環など）。
3. フラグメントを展開して数えた深さ5以下、別名15個以下、一覧の要求件数50件以下（リテラルでも変数でも）、計算量300以下。計算量は項目ごとに1で、一覧の項目は要求件数を掛ける。

深い入れ子、広い一覧、別名の大量複製、フラグメントによる増幅、変数経由の大量要求、トークンやバイトの超過を試験で拒否する（`tests/test_graphql_api.py`）。

## GraphQL の資源単位の認可（task.optional_more_protocols.003）

主体はホストが発行した資格情報から決め、問い合わせの本文や変数からは受け付けない。仕事のリゾルバーは呼出し主体の仕事だけを返し、他の主体の仕事は存在しない識別子と同じ `null` になる。識別子指定、変数、別名、フラグメント、インラインフラグメント、一覧のいずれでも他人のデータは返らない。所有者はスキーマの項目にないため、他の主体の名前を問い合わせで指定する方法もない。

## gRPC が必要なプロセス間通信（task.optional_more_protocols.004）

現在のプロセス間通信は次の4つで、すべて同じホストの中に閉じている。

| 経路 | 実装 | 相手 |
| --- | --- | --- |
| 標準入出力の JSON-RPC（MCP） | `server_mcp.py` | MCP クライアントが起動する1プロセス |
| ループバックの HTTP | `src/verified_execution/http_local.py` | 同じ端末の診断クライアント |
| SQLite の台帳ファイル | `src/verified_execution/worker_leases.py` | 同じホストのワーカープロセス（[multiprocess_stress.json](../../evidence/multiprocess_stress.json)） |
| 子プロセスの JSON 行 | `src/verified_execution/recorded_cli.py` | 試験用の子プロセス |

別のホストに独立して配置されるサービスがないため、gRPC は導入しない。導入を検討する条件は、別々に配置・更新されるサービスの間で、型付きの双方向ストリームが必要になったとき。どちらのロックにも gRPC と Protocol Buffers の実装は入っていない。

## Protocol Buffers の版互換試験（task.optional_more_protocols.005）

gRPC を導入する場合に、新しい `.proto` を公開する前に通すべき試験を [proto_compat.py](../../src/vater_optional/proto_compat.py) に定義した。旧版の各項目番号について、次のいずれかがあれば互換性を壊すと判定する。

- メッセージや項目を、番号と名前の両方を `reserved` にせずに削除した。
- 型、または単数・optional・repeated の区別を変えた。
- 名前を変えた（バイナリ形式は保たれるが、JSON とテキスト形式が壊れる）。
- 旧版が予約した番号や名前を新しい項目が使った。

新しい番号で項目を追加するのは互換（旧版の読み手は読み飛ばす）。入れ子のメッセージ、oneof、map、enum、import、proto2 の required は「未対応」として拒否し、検査せずに通すことはしない。

## Agent2Agent が必要な委任処理（task.optional_more_protocols.006）

実行主体は1つで、MCP（`server_mcp.py`）、ローカル HTTP（`http_local.py`）、ローカルの行入力（`local_cli.py`）のどの入口も、同じホストの同じ `ToolRuntime`（旧名 `CalculationService`）を呼ぶ。運用者や信頼の範囲が異なる独立した実行主体が複数存在しないため、委任の相手がおらず、Agent2Agent は導入しない。別の運用者が持つエージェントへ仕事を任せる必要が生じた時点で、次節の条件で委任を設計する。

## Agent2Agent の承認と費用委譲（task.optional_more_protocols.007）

[delegation.py](../../src/vater_optional/delegation.py) が委任の規則を定める。受託先へ渡す権限は、必ず委任元より狭い。

- ツール：委任元のツールの、明示した空でない部分集合。`*` や `all` のような全部の指定は拒否する。
- 費用：正の金額を委任元の残額から取り分ける（複製しない）。委任元と受託先を合わせても、委任元の元の予算を超えて使えない。
- 期限：委任元より遅くならず、1時間以内。
- 再委任：委任元が許し、連鎖の深さが上限未満のときだけ。
- 承認：人間の承認は引き継がない。承認が必要なツールは、連鎖の起点の人間が受託先のために改めて承認する。委任元のエージェントや通信文が承認済みと主張しても通らない。

## エディター接続規格（task.optional_more_protocols.008）

対象エディターは、MCP クライアントとして接続する Claude Code と Visual Studio Code に決めた。どちらも標準入出力の MCP サーバー（`server_mcp.py`）に接続でき、公式 Python SDK のクライアントによる実接続試験と、公式の適合性検査（[results.json](../../evidence/mcp-conformance/results.json)）で `tools-list`、`tools-call-simple-text`、`tools-call-error` が成功している。ツール呼出しは MCP で足りるため、Language Server Protocol や Agent Client Protocol のような別のエディター接続規格は調査も実装もしない。MCP に対応しないエディターを対象に加える場合に限り、その時点で調査する。
