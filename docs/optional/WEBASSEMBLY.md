# WebAssembly 隔離の試作（任意段階 `optional_webassembly`）

実装は [wasm_sandbox.py](../../src/vater_optional/wasm_sandbox.py)、試験は [test_wasm_sandbox.py](../../tests/test_wasm_sandbox.py)。wasmtime 50.0.1 は任意試作用のロック [requirements-optional-py313.lock](../../requirements-optional-py313.lock) にだけ入り、リリースのロックとパッケージには入らない。試験は CI の `optional-prototypes` ジョブで実行する。

## 外部作成コードを実行する必要性（task.optional_webassembly.001）

現在の実行中核が実行するのは、リポジトリ内で審査した Python の処理（加算、台帳経由の外部変更）だけで、利用者や第三者が書いたコードを受け取って実行する入口はない。必要性がないため、リリースには導入しない。この試作は、将来その入口を作る場合に隔離へ何が必要で、どれだけの費用がかかるかを実測するためのもの。`pyproject.toml` がパッケージに含めるのは `verified_execution` と `verified_execution_contracts` だけで、`vater_optional` と wasmtime はリリースに入らないことを試験で確かめる。

## 候補の比較と選定（task.optional_webassembly.002）

| 候補 | 計算量の上限 | 時間の上限 | メモリ上限 | Python から使う方法 | 判断 |
| --- | --- | --- | --- | --- | --- |
| wasmtime | fuel（命令数） | epoch 割込み | store の上限（メモリ・表・実体数） | 公式の Python バインディング（PyPI の wheel、依存なし） | 採用 |
| wasmer | metering ミドルウェア | なし（別スレッドで停止） | 独自の tunables | Python バインディングの更新が止まっている | 不採用 |
| wasm3（pywasm3） | なし | なし | 限定的 | インタープリター、保守が停滞 | 不採用 |
| Pyodide / ブラウザーの V8 | なし（ページ単位） | タブ単位 | ブラウザー任せ | ホストの Python から直接使えない | 不採用 |

wasmtime を選んだ理由は、計算量・時間・メモリの3種の上限を API で個別に掛けられること、WASI を定義しない限りファイルやネットワークへの経路が存在しないこと、Bytecode Alliance の脆弱性対応手順と継続的なファジングがあること。採用版は公開から2週間以上経った 50.0.1。

## 脅威モデル

守る資産は、ホストのプロセスが持つ台帳、資格情報（環境変数を含む）、ファイル、ネットワーク、計算資源。攻撃者は承認済みモジュールに悪意ある入力を与える者と、モジュールそのものを差し替えようとする者。

| 脅威 | 対策 | 試験 |
| --- | --- | --- |
| 未承認や改変したモジュールを起動させる | SHA-256 の承認一覧にないものはコンパイルもしない。テキスト形式や事前コンパイル済みコードは受け付けない | `test_an_unapproved_or_altered_module_is_never_compiled` |
| 許可していないホスト関数を呼ぶ | 審査済み一覧にあり、その実行で許可したものだけをリンクする。型が違えば拒否 | `test_only_reviewed_and_granted_host_functions_can_be_imported`, `test_a_reviewed_import_with_another_type_is_refused` |
| ファイル・ネットワーク・環境変数・時計・乱数に触れる | WASI を定義しない。WASI の取込みを要求するモジュールは起動しない | `test_files_network_and_environment_are_not_reachable` |
| 秘密情報を読み出す | 入力以外のデータを渡さない。実行ごとに新しいメモリを使う | `test_no_host_secret_reaches_the_guest` |
| 無限ループで計算資源を占有する | fuel で命令数を制限する | `test_an_endless_loop_ends_at_the_compute_limit` |
| 大量のメモリや表を確保する | store の上限で拒否する | `test_large_memory_and_table_requests_are_refused` |
| 時間超過で結果があいまいになる | epoch の期限で止め、外部効果の前後で `interrupted` と `unknown` を分ける | `test_a_timeout_is_interrupted_before_effects_and_unknown_after_them`, `test_fuel_exhaustion_after_an_effect_is_unknown_too` |
| 不正な型の値で境界をすり抜ける | 宣言した型と範囲を実行前に検査し、出力の形も検査する | `test_input_of_the_wrong_type_or_range_is_refused_before_the_guest_runs`, `test_the_interface_types_are_checked_both_ways` |
| 再帰・範囲外出力・範囲外の emit で外へ影響する | スタック上限、出力位置と長さの検査、emit の範囲検査 | `test_resource_exhaustion_and_escape_attempts_stay_inside` |
| wasmtime 自体の脆弱性でプロセスを乗っ取る | 別プロセス（空の環境、ファイル書込み不可、CPU 上限）で実行する | `test_the_isolated_process_holds_no_credentials_and_cannot_write_files` |

## 許可する機能と権限（task.optional_webassembly.003 / 004 / 005 / 009）

審査済みのホスト関数は2つだけ。

- `vater.emit`(ptr, len) → i32。外部効果あり。ゲスト自身のメモリの [ptr, ptr+len)（1回4 KiB まで）をホストの受け口へ渡す。外へ出る唯一の経路なので、これを呼んだ後に止まった実行は `unknown` になる。
- `vater.fail`(code)。外部効果なし。ゲストが入力を拒否したことを伝えて実行を終える。

実行ごとに許可するのは、モジュールが取り込み、かつホストが許可した関数だけで、`Outcome.granted` に一覧が残る。WASI（`wasi_snapshot_preview1`、`wasi_unstable`、`wasi`）と C 実行時の慣例的な取込み（`env`）は一切定義しないため、ファイル、ソケット、時計、乱数、環境変数、起動引数は既定で使えない。ゲストが受け取るデータは入力だけなので、秘密情報を渡す経路がない。

## 上限と中断（task.optional_webassembly.006 / 007 / 008）

既定値は fuel 5,000,000、線形メモリ 256 KiB（4ページ）、表の要素 1,000、実体・表・メモリ各1、スタック 256 KiB、時間 1 秒。時間は 5 ミリ秒ごとに進む epoch で判定する。終わり方は次のとおり区別する。

- `completed`：出力を型どおりに読めた。
- `refused`：何も実行していない（未承認、取込み不許可、入力の型違反、メモリ宣言の超過）。
- `guest_error`：ゲストの誤り（トラップ、`vater.fail`、出力の形の違反）。
- `fuel_exhausted`：計算量の上限に達した。
- `interrupted`：時間の上限に達した。外部効果のある関数は呼ばれていないので、外では何も起きていない。
- `unknown`：外部効果のある関数を呼んだ後に異常終了した。どの効果まで意図されていたか分からないため、渡された効果（`Outcome.emitted`）から照合し、そのまま再試行しない。

## 部品間のデータ型（task.optional_webassembly.010）

ABI は `vater-wasm-abi/1`。ゲストは `memory`、`alloc(i32) -> i32`、`run(i32, i32) -> i64` を正確にこの型で公開する。入出力は宣言した固定長のリトルエンディアン整数（u8、u32、u64、i64）と、最後に1つだけ置ける可変長バイト列。入力は型と範囲を実行前に検査し（真偽値や浮動小数点は拒否）、出力は長さが宣言と一致しなければ誤りとする。

## 署名またはハッシュの検査（task.optional_webassembly.011）

起動してよいモジュールは SHA-256 で承認する（[approved.json](../../tests/fixtures/wasm/approved.json)）。一覧にないモジュールはコンパイルもしない。1バイトでも変われば別のモジュールで、承認し直す必要がある。承認済みの値でも、テキスト形式や事前コンパイル済みのネイティブコード（`Module.deserialize`）は受け付けない。

## OS による追加隔離（task.optional_webassembly.012）

wasmtime はホストと同じプロセスで動くため、wasmtime 自体に脱出できる脆弱性があれば、ホストの台帳や資格情報に届く。第三者が書いたモジュールを受け付ける場合は追加の隔離が必要と判断する。試作の `run_isolated` は、空の環境変数、ファイル書込み不可（`RLIMIT_FSIZE` 0）、ファイル記述子32、CPU 時間の上限を掛けた子プロセスで同じ検査と実行を行う。ネットワーク名前空間や seccomp による遮断はこの試作に含まれず、受け付ける前に別途必要。子プロセスが結果を返さずに終わった場合は、外部効果のある関数を許可していれば `unknown`、そうでなければ `interrupted` とし、完了とは扱わない。

## 資源枯渇と情報流出（task.optional_webassembly.013）

無限ループ、メモリと表の大量確保、深い再帰、範囲外の出力と emit、メモリ全体の読み出しをそれぞれ試験し、いずれも許可外の操作にならないことを確かめた。実行ごとに新しい store を使うので、前の実行の入力は次の実行のメモリに残らない。

## 運用負担の比較（task.optional_webassembly.014）

[webassembly-cost.json](../../evidence/optional/webassembly-cost.json)（`scripts/measure_wasm_cost.py`、Python 3.13、x86_64、各5回の中央値）による同じ加算1回あたりの時間。

| 経路 | 1回あたり | Python 中核との比 |
| --- | --- | --- |
| Python 中核（`core.checked_add`） | 0.1 マイクロ秒 | 1 |
| WebAssembly、同一プロセス（呼出しごとに新しい store） | 118.0 マイクロ秒 | 約870倍 |
| WebAssembly、別プロセス（`run_isolated`） | 69287.4 マイクロ秒 | 約51万倍 |

モジュールのコンパイルは 2.8 ミリ秒。追加の依存は wasmtime 1件で、導入サイズは 30,232,307 バイト（ネイティブライブラリを含む）。試作のコードは 419 行。

セキュリティ面では、Python 中核は外部作成コードを実行しないため、この表の脅威はそもそも存在しない。WebAssembly で外部コードを受け付けると、ホスト関数2つ（外部効果あり1つ）、承認・形式・型・取込み・入力の5段の検査、wasmtime と別プロセスの境界を保守する必要が生じる。外部作成コードを実行する必要が生じるまでは Python 中核だけを使い、必要になれば別プロセスでの実行を前提に、1回あたり約 69 ミリ秒の費用を見込む。
