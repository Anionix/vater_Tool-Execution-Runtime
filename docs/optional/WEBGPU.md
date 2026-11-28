# WebGPU 高速化の試作（任意段階 `optional_webgpu`）

シェーダーは [matmul.wgsl](../../web/webgpu/matmul.wgsl)、ページ側の検査と計測は [matmul.js](../../web/webgpu/matmul.js)、CPU 基準実装とホストの検査は [matmul.py](../../src/vater_optional/matmul.py)、試験は [test_webgpu.py](../../tests/test_webgpu.py)。この環境と CI には GPU がないため、Chromium 158 の WebGPU を SwiftShader（CPU で動くソフトウェアの GPU、`isFallbackAdapter` が真）で実行した。シェーダーの正しさと検査は実際に動かして確かめたが、速度の数値は本物の GPU の性能を表さない。

## 対象の計算（task.optional_webgpu.001）

対象は正方行列の積 C = A × B（行優先の float32、1 ≤ n ≤ 512）。要素ごとに独立した n 回の積和で、大量並列計算の典型。

## CPU 基準実装（task.optional_webgpu.002）

`reference()` は入力を float32 の値に限り、各要素の n 個の積を float64 の `math.fsum` で正確に足してから float32 へ1回だけ丸める。`generate()` が作る入力は [-1, 1] の 1/1024 の倍数で、すべての積と和が float64 で正確に表せるため、基準実装の値は正確な和を float32 に丸めたものと一致する。入力は乱数の種で固定する。

## 基準実装の速度とメモリ（task.optional_webgpu.003）

[webgpu-measurements.json](../../evidence/optional/webgpu-measurements.json)（`scripts/measure_webgpu.py`）による Python 基準実装の時間と最大メモリ（tracemalloc）。

| n | 時間 | 最大メモリ |
| --- | --- | --- |
| 32 | 9.8 ミリ秒 | 42,907 バイト |
| 64 | 35.6 ミリ秒 | 163,344 バイト |
| 128 | 229.3 ミリ秒 | 664,724 バイト |

## GPU へ渡すデータの分類（task.optional_webgpu.004）

| 分類 | GPU へ送る | 理由 |
| --- | --- | --- |
| public | 可 | 公開してよいデータ |
| synthetic | 可 | 試験用に生成したデータ |
| internal | 不可 | GPU のメモリは使用後に消去される保証がなく、ドライバーやブラウザーの別プロセスを経由する |
| confidential | 不可 | 同上 |
| personal | 不可 | 同上 |

未知の分類は拒否する（`gpu_destination_allowed()`）。

## 計算用シェーダー（task.optional_webgpu.005）

入力は読み取り専用の `array<f32>` が2つ（binding 0 と 1）、出力は `array<f32>`（binding 2、read_write）、大きさは uniform の `u32`（binding 3）。作業グループは 8 × 8。

## バッファの大きさと範囲外アクセス（task.optional_webgpu.006）

バッファを作る前に、ページ（`validateInput()`）とホスト（`validate()`）が、n が 1〜512 の整数であること、各行列の長さが n × n であること、値が有限の float32 であることを検査し、違反すれば `invalid_size`、`invalid_length`、`not_float32`、`not_finite` で拒否する。シェーダーは行列の外にある呼出しをメモリに触れる前に終える。8 の倍数でない n でも結果が正しいことを試験する。

## 浮動小数点の誤差条件（task.optional_webgpu.007）

許容誤差は、どの順で足しても成り立つ float32 の積和の誤差の上限とする。

    |GPU の値 - 正確な値| <= n × 2^-24 × Σ_k |a_ik × b_kj| + 2^-24 × |正確な値|

第1項は n 回の float32 の加算の誤差の上限（γ_n）、第2項は最後の丸め。

## CPU と GPU の結果の比較（task.optional_webgpu.008）

GPU の結果をホストで基準実装と比べ、全要素が許容誤差の内側にあることを確かめた。計測した3つの大きさでは誤差と許容誤差の比の最大が 0.007561363038867461（n = 128）。1/1024 の倍数でない一般の float32 入力と、8 の倍数でない大きさでも試験する。

## 転送時間込みの速度（task.optional_webgpu.009）

GPU の時間は、バッファの作成、書込み、実行、読戻しまでを含む往復（5回の中央値）。CPU はページ内の JavaScript の単純なループと Python 基準実装。

| n | GPU（転送込み） | JavaScript の CPU | Python 基準実装 |
| --- | --- | --- | --- |
| 32 | 2.7 ミリ秒 | 0.3 ミリ秒 | 9.8 ミリ秒 |
| 64 | 2.6 ミリ秒 | 0.9 ミリ秒 | 35.6 ミリ秒 |
| 128 | 7.1 ミリ秒 | 7 ミリ秒 | 229.3 ミリ秒 |

総処理時間は、最も速い CPU の経路（JavaScript）に対して改善しなかった。SwiftShader は CPU 上で GPU を模倣するため、本物の GPU での値ではない。

## デバイス喪失時の処理（task.optional_webgpu.010）

デバイスを失った場合は新しいデバイスを1回だけ要求し（`MAX_DEVICE_RETRIES` は 1）、それでも失えば CPU で計算する。無限に再試行しない。毎回失う場合はデバイスの要求が2回で止まり、1回目だけ失う場合は2回目の GPU で結果を返すことを試験する。

## GPU はセキュリティ境界ではない（task.optional_webgpu.011）

GPU は隔離のための仕組みではない。隔離と認可の責任は Python ホストの共通中核にあり、GPU で計算するかどうか、どのデータを渡すかはホストが決める。GPU が返した値は信頼しない入力として扱い、ホストの `accept_gpu_result()` が長さ、有限性、許容誤差を検査してから受け入れる。ページは資格情報を持たない。

## 撤去の条件（task.optional_webgpu.012）

採否は計測値で決める。WebGPU を残すのは、次をすべて満たす場合だけ。

- ハードウェアの GPU（`isFallbackAdapter` が偽のアダプター）で計測している。
- 計測したすべての大きさで、転送込みの往復が最も速い CPU の経路の 2 倍以上速い。
- すべての結果が許容誤差の内側にある。

今回の計測では、アダプターが SwiftShader（ハードウェアではない）、往復は CPU より速くならず、結果は許容誤差の内側だった。規則により採用しない。この試作はリリースに入れず、ハードウェアの GPU で同じ計測をして条件を満たすまで、高速化の手段として扱わない。
