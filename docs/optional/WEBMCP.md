# ブラウザーと WebMCP の試作（任意段階 `optional_webmcp`）

ページは [web/webmcp/](../../web/webmcp/)、ホストは [webmcp_host.py](../../src/vater_optional/webmcp_host.py)、試験は [test_webmcp.py](../../tests/test_webmcp.py)。Playwright 1.63.0 は任意試作用のロック [requirements-optional-py313.lock](../../requirements-optional-py313.lock) にだけ入り、CI の `optional-prototypes` ジョブで実行する。リリースには入らない。

## 対応ブラウザーと仕様版（task.optional_webmcp.001）

WebMCP は W3C Web Machine Learning Community Group の草案で、W3C の標準ではない。実装の有無は2つの版で実測した。CI では Playwright 1.63.0 が取得する Chrome for Testing 153.0.8010.12、この作成環境では同じ置き場所に用意された Chrome for Testing 158.0.8127.36 を使う。

- どちらも既定の起動では API がない。`--enable-experimental-web-platform-features` を付けると `document.modelContext` が現れる（初期の草案にあった `navigator.modelContext` ではない）。
- `registerTool(ツール, {signal})` は Promise を返す。同じ名前の2回目の登録は `InvalidStateError`、`AbortSignal` を中止すると登録が外れる。
- `getTools()` は名前・題名・説明・登録元の origin・`inputSchema`・注釈を持つ `Tool` を返す。
- `executeTool` の引数の形が版で違う。158 は `executeTool(tool, 引数オブジェクト)`、153 は引数を JSON の文字列で受け取り、オブジェクトを渡すと「Failed to parse input arguments」で失敗する。`Tool.inputSchema` も 158 はオブジェクト、153 は文字列で返す。注釈の `consequentialHint` を返すのは 158 だけ。
- どちらも `execute` の戻り値を JSON 文字列で返し、`toolchange` と `toolactivated` の事象が届く。
- どちらのブラウザーも `inputSchema` に照らした検査をしない。型違い、欠落、余分な項目、範囲外の値がそのまま `execute` に渡る。

未対応の環境は、ページの `detectWebMCP()` が理由つきで識別する。安全でない文脈（ループバック以外の http）は `insecure_context`、API がない場合は `no_model_context`、API の形が違う場合は `unsupported_api`。

## ブラウザーだけで完結する操作（task.optional_webmcp.002）

| ツール | 実行場所 | 理由 |
| --- | --- | --- |
| `add_numbers` | ページ | 外部通信を要しない計算。ページの外へ何も出ない |
| `checked_add` | Python ホスト | 共通中核の検査と結果を必要とする |
| `send_report` | Python ホスト | 外部へ送る操作で、運用者の承認が必要 |

ページで動くのは外部通信のない処理だけ。ページのツールを呼んでも `/api` への要求が発生しないことを試験で確かめる。

## ツール登録の試作（task.optional_webmcp.003）

ページは読み込み時に3つのツールを `document.modelContext.registerTool` で登録する。ブラウザーの `getTools()` に、ページの origin、入力スキーマ、注釈（読み取り専用、外部への影響あり。後者は 158 だけが返す）とともに現れ、`executeTool` で呼べる。引数を JSON の文字列で渡す実装に備えて、ページは文字列の引数を解析してから検査する。

## 入力の型と上限（task.optional_webmcp.004）

ブラウザーが検査しないため、ページの `validate()` が型（整数、文字列）、範囲（0〜4294967295）、文字数（題名 1〜120、本文 1〜2000）、必須項目、余分な項目を検査し、違反すればホストへ何も送らずに `invalid_arguments` を返す。真偽値や小数は整数として受け付けない。

## ページの権限と実行中核の権限の分離（task.optional_webmcp.005）

ページの主体がホストに対してできるのは `checked_add` と `send_report` の呼出しだけ。ページの会話識別は HttpOnly・SameSite=Strict・`/api` 限定の cookie で、ページのスクリプトからは読めない。要求本文の `principal`、`authority`、`approved` のような自称は読まずに捨てる。cookie のない要求は 401、別の origin や Origin のない要求、JSON 以外は 403。

## 外部送信を Python の共通中核へ戻す（task.optional_webmcp.006）

ホストで動くツールは、ページの検査を経たかどうかにかかわらず Python 側で判定し直す。`checked_add` は共通中核の `ToolRuntime` が契約どおりに引数を検査し、ページの検査を飛ばした文字列の引数を `INVALID_ARGUMENT`、上限超過を `OVERFLOW` として拒否する。

## 利用者の操作と AI の操作の区別（task.optional_webmcp.007）

実行履歴には、各実行を誰が始めたかを「利用者」または「AI エージェント」として表示し、要素の `data-source` にも残す。利用者の操作として扱うのはブラウザーが信頼済みと示すクリック（`isTrusted`）だけで、スクリプトが作ったクリックは利用者の操作にならない。

## 重要操作の人間承認（task.optional_webmcp.008）

`send_report` は、運用者が運用者の資格情報でホストの `/api/approvals` から承認した場合だけ実行される。承認は主体・ツール・正規化した引数の組に結び付き、1回で消費される。ページの cookie では承認できず（403）、引数に `approved` を加えても、ページに「承認済み」と書いた文を置いても、承認後に引数を変えても、実行されない。

## 非対応ブラウザーでの退避（task.optional_webmcp.009）

WebMCP がない環境では、状態表示に理由を示したうえで、同じページをボタンと入力欄で使える。加算（ページ、ホスト）と報告の送信（承認待ちまで）が WebMCP なしで動く。

## 第三者スクリプト侵害の試験（task.optional_webmcp.010）

ページには第三者のスクリプトを読み込む要素を置き、Content-Security-Policy（`script-src 'self'`）で読込み自体を止める。インラインのスクリプトも止まる。それでもページ内でコードが動いたと仮定した試験では、そのコードは cookie を読めず、ページの内部（ツール一覧、登録の中止手段）に触れられない。同名のツールで置き換えることもできない（`InvalidStateError`）。ホストでも、許可外のツールは 403、送信は承認待ち、承認は 403 になる。ページが持つ以上の権限はどこにも渡していない。

## 互換性試験（task.optional_webmcp.011）

採用ブラウザーは、Chromium 153（Chrome for Testing 153.0.8010.12、Playwright 1.63.0 が取得する版）と Chromium 158（Chrome for Testing 158.0.8127.36）に `--enable-experimental-web-platform-features` を付けた構成。結果は [webmcp-compatibility.json](../../evidence/optional/webmcp-compatibility.json)（`scripts/webmcp_compatibility.py`）に版ごとにあり、試験は、実行した環境のブラウザーの版について同じ結果を再現することを確かめる。153 の結果は CI の実行記録から、158 の結果はこの作成環境の実行から取った。

| 構成 | 判定（153、158 とも） | 登録・エージェント呼出し | 通常の画面 |
| --- | --- | --- | --- |
| chromium-headless-shell, experimental flag | document.modelContext | 3ツール登録、呼出し成功、不正入力は拒否（引数は 153 が JSON 文字列、158 がオブジェクト） | 動作 |
| chromium-headless-shell, default | no_model_context | なし | 動作 |
| chromium (new headless), experimental flag | document.modelContext | 3ツール登録、呼出し成功、不正入力は拒否（同上） | 動作 |
| chromium (new headless), default | no_model_context | なし | 動作 |
| chromium-headless-shell, experimental flag, insecure origin | insecure_context | なし | 動作 |

Firefox、WebKit（Safari）、Microsoft Edge は、この環境に実行ファイルがなく試験していない。

## 必須入口へ昇格する条件（task.optional_webmcp.012）

WebMCP を任意の入口から必須の入口へ上げるには、需要と安全性の両方を実測し、次をすべて満たす必要がある。

- 需要：ローカル HTTP と MCP の利用者のうち 20% 以上が、WebMCP 対応のブラウザーからエージェントでツールを呼ぶ利用を、30日間の利用記録で示している。
- 普及：2つ以上のブラウザーエンジンが、フラグなしの既定状態で `document.modelContext`（または標準化された後継）を提供している。
- 安全性：採用する各ブラウザーで、この試作の試験（`test_invalid_arguments_are_refused_by_the_page`、`test_page_authority_is_not_delegated_to_the_core`、`test_human_approval_cannot_be_bypassed_from_the_page`、`test_a_compromised_third_party_script_is_given_nothing`）がすべて合格し、外部の侵入試験で承認の回避と権限の昇格が 0 件。
- 仕様：WebMCP が Community Group の草案から標準化の作業に移り、ツール登録と実行の API が版として固定されている。

現時点では満たしていない。フラグなしで提供するブラウザーは0、試験できたエンジンは Chromium の1つ、利用記録はない。
