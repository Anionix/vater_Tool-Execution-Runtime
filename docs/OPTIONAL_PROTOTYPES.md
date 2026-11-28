# 任意段階の試作

任意段階（`optional_webassembly`、`optional_more_protocols`、`optional_webmcp`、`optional_webgpu`）の試作と判断の索引。試作のコードは `src/vater_optional/` と `web/` にあり、`pyproject.toml` がパッケージに含めるのは `verified_execution` と `verified_execution_contracts` だけなので、リリースには入らない。

| 段階 | 試作 | 判断 | 記録 |
| --- | --- | --- | --- |
| WebAssembly 隔離 | wasmtime 50.0.1 の隔離（承認済みハッシュ、WASI なし、審査済みホスト関数、計算量・メモリ・時間の上限、別プロセス） | 外部作成コードを実行する必要がないため、リリースには導入しない | [WEBASSEMBLY.md](optional/WEBASSEMBLY.md) |
| 追加通信規格 | GraphQL の上限と主体ごとの読取り、.proto の版互換検査、委任の規則 | GraphQL・gRPC・Agent2Agent・追加のエディター接続規格は導入しない | [MORE_PROTOCOLS.md](optional/MORE_PROTOCOLS.md) |
| WebMCP | Chromium 158 の `document.modelContext` へのツール登録と、判定を担う Python ホスト | 実験用フラグが必要で、昇格の条件を満たさない。任意の入口に留める | [WEBMCP.md](optional/WEBMCP.md) |
| WebGPU | WGSL の行列積と正確な CPU 基準実装 | ソフトウェアのアダプターで、転送込みで CPU より速くならない。採用しない | [WEBGPU.md](optional/WEBGPU.md) |

## 依存と実行

試作の依存は [requirements-optional-py313.lock](../requirements-optional-py313.lock)（基本のロックをそのまま含み、wasmtime 50.0.1、graphql-core 3.3.2、Playwright 1.63.0 を加えたもの）に固定する。入力は [requirements-optional.in](../requirements-optional.in)。基本のロックとの一致は `tests/test_dependency_sources.py`、脆弱性の照会は CI の `dependency-vulnerabilities` ジョブが受け持つ。

CI の `optional-prototypes` ジョブが、このロックをハッシュ付きで導入し、固定した Playwright が指定する Chromium を取得して、4段階の試験を `scripts/verify_phase.py` で実行する。手元では次の順に実行する。

    python -m pip install --require-hashes --only-binary :all: -r requirements-optional-py313.lock
    python -m playwright install --with-deps chromium
    python scripts/verify_phase.py optional_webassembly

計測値は `evidence/optional/` にあり、それぞれ `scripts/measure_wasm_cost.py`、`scripts/measure_graphql_demand.py`、`scripts/webmcp_compatibility.py`、`scripts/measure_webgpu.py` で作り直せる。
