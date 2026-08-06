# ADR-004: 構造化ログを Lambda Powertools Logger へ移行する

| 項目 | 内容 |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-08-05 |
| **関連 PR** | PR #3 (powertools-observability) |
| **Resolves** | ADR-003 §5.2 の残課題「print は暫定、PR#3 で置換」 |

## 1. 背景

ADR-003 で例外伝播を導入した際、構造化 ERROR ログを
`print(json.dumps({...}))` で手書きした。これは意図的な暫定実装であり、
「PR#3 で Powertools Logger に置換する」と出口を明記していた。

## 2. 問題

手書きの構造化ログには3つの弱点がある。

1. **書式が呼び出し箇所ごとにブレる**: `level` を `severity` と書けば
   CloudWatch Logs Insights の集計から漏れるが、コードは正常に動く。
2. **正常系が構造化されない**: ERROR だけ JSON、INFO は素の print という
   非対称。`filter level = "INFO"` のような一貫したクエリができない。
3. **相関 ID がない**: 同一実行のログを紐付ける手段が存在せず、
   並行実行時にどのログがどの起動に属するか判別できない。

## 3. 決定

`aws-lambda-powertools` の `Logger` を導入し、
`print` によるログ出力をすべて置換する。

- `Logger(service="rss-collector")` — service 名が全レコードに自動付与され、
  複数関数を横断した絞り込みが可能になる。
- `logger.exception()` — severity=ERROR とスタックトレースを自動付与。
- 追加フィールドは `extra={}` で渡し、JSON のルートに展開される。

### スコープ外（意図的な保留）

`@logger.inject_lambda_context` は本 PR に含めない。
このデコレータは `context.aws_request_id` を参照するが、現行のテストは
`lambda_handler(event, None)` と呼び出しており、context オブジェクトを
提供していない。**相関 ID の導入はテストハーネスの変更を伴うため、
後続 PR で独立して扱う**。

## 4. 結果

### ポジティブ
- ログ書式の一貫性がライブラリによって保証される
- 正常系も含めて全ログが JSON 化され、Logs Insights のクエリが統一される
- `service` によるフィルタリングが可能になる

### ネガティブ / 残課題
- デプロイパッケージサイズが増加する（Powertools 分）
- **相関 ID (request_id) は未導入** — 後続 PR のスコープ
- Metrics / Tracer は本 PR で扱わない

## 5. 教訓

**暫定実装には出口を刻み、実際に出口へ到達させる**。
ADR-003 が「PR#3 で置換」と書いていたからこそ、本 PR は
「思いつきの改善」ではなく「予告された返済」として実行できた。
出口を書かない暫定実装は、暫定のまま恒久化する。

### テスト実装上の発見

`capsys` では Powertools のログを捕捉できない。Powertools は
モジュール import 時に `StreamHandler(sys.stdout)` を生成するため、
`capsys` による `sys.stdout` 差し替えより先に元のストリームを掴んでいる。
ファイルディスクリプタレベルで捕捉する `capfd` を使う必要がある。
