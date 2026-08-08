# ADR-005: EventBridge event id を相関IDとして採用する

| 項目 | 内容 |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-08-08 |
| **関連 PR** | PR #4 (correlation-id) |
| **Resolves** | ADR-004「スコープ外(意図的な保留)」の inject_lambda_context |

## 1. 背景

ADR-004 で Powertools Logger へ移行した際、`@logger.inject_lambda_context` は
意図的にスコープ外とした。理由は「現行テストが context=None を渡しており、
テストハーネスの変更を伴う」ため。本 ADR はその返済である。

## 2. 問題

構造化ログは実現したが、**同一実行のログを紐付ける手段がない**。
とくに ADR-003 で Lambda の自動リトライ(最大2回)を有効化したため、
1つの障害に対して最大3回分のログが出力される。
相関IDがなければ、どのログがどの試行に属するか判別できない。

## 3. 決定

`@logger.inject_lambda_context(correlation_id_path=correlation_paths.EVENT_BRIDGE)`
を採用し、**EventBridge の `event['id']`** を相関IDとする。

### なぜ aws_request_id ではなく event id か

`context.aws_request_id` は **試行ごとに変わる**。
一方 EventBridge の `event['id']` は **リトライ時も不変**である。

| 候補 | リトライ間の値 | 追跡できるもの |
|---|---|---|
| `aws_request_id` | 毎回変わる | 1回の試行 |
| **`event['id']`** | **不変** | **同一イベントの全試行** |

ADR-002 が `event['time']` を冪等キーの源泉に選んだのと同じ理由——
**リトライ間で不変な値を選ぶ**——が、観測性にも適用される。

## 4. 結果

### ポジティブ
- Logs Insights で `filter correlation_id = "..."` により、
  同一イベントの3回の試行を一度に取り出せる
- cold start / function_name / memory_limit も自動付与される

### ネガティブ / 残課題
- テストは Lambda context のスタブを必要とするようになった
- Metrics / Tracer は依然として未導入

## 5. 教訓

**「リトライ間で不変な値を選ぶ」という判断は、冪等性と観測性の両方に効く**。
ADR-002 で S3 キーの源泉を選んだときと同じ基準が、
相関IDの選択でもそのまま使えた。設計原則が層をまたいで再利用できるとき、
それは筋の良い原則である。

### 作業上の発見: PowerShell による日本語ファイル編集

本 PR の作業中、`Get-Content -Raw | Set-Content` による一括置換で
テストファイルの日本語コメントが全て文字化けした。PowerShell 5.1 は
UTF-8 ファイルを既定コードページ(Shift-JIS)として読むため、
再書き込み時に破壊される。

`git checkout` で復元後、以下の方法で解決した。

    $t = [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8)
    $t = $t.Replace("...", "...")
    [System.IO.File]::WriteAllText($path, $t, (New-Object System.Text.UTF8Encoding($false)))

**日本語を含むファイルの機械的編集はエンコーディングを明示する**。
`pytest.ini` が BOM 付き UTF-8 を拒否した件と同根の問題であり、
Windows 環境でのファイル操作は常にエンコーディングを疑う。