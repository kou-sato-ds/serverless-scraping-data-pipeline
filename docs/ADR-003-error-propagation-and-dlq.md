# ADR-003: handler の例外伝播と SQS Dead Letter Queue によるサイレント失敗の廃絶

| 項目 | 内容 |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-07-12 |
| **Decision Makers** | プロジェクトオーナー |
| **関連 Issue / PR** | PR #2 (error-propagation-dlq) |
| **Depends on** | ADR-002 (Content-Addressable キー = リトライ安全性の前提) |

---

## 1. 背景 (Context)

v2.0 (PR #1) 時点の `lambda_handler` は、例外を捕捉して `{"statusCode": 500}` を
return する。これは API Gateway の**同期呼び出し**向けの慣習であり、本パイプラインの
起動経路である EventBridge → Lambda の **async invocation** では意味を持たない。

なお本改修を PR #1 より**後**に置いたのは意図的である。リトライの有効化は、冪等性の
土台なしではデータ重複を悪化させる (ADR-002 §2.1 シナリオ A)。土台が main に
マージされた今、初めて本改修が安全になった。

## 2. 検出した問題 (Problem)

async invocation では「関数が例外で終了したか」だけが成否判定である。
500 を return しても **AWS からは成功**として扱われ、以下の全機構が死んだままになる:

| 障害時に期待される機構 | v2.0 での実際 |
|---|---|
| Lambda 自動リトライ (最大2回) | 発動しない (成功扱い) |
| `AWS/Lambda` Errors メトリクス | 増えない |
| OnFailure Destination / DLQ | 送られない |
| CloudWatch Alarm → 通知 | 鳴らない |

結果: RSS 取得が数日連続で失敗しても誰も気づかない (**サイレント失敗**)。
ADR-001 の教訓 1 (「動いた」と「正しい」は違う) と同型の問題が運用層で再発している。

## 3. 検討した選択肢 (Options Considered)

### Option A: 現状維持 (500 return)
- **却下**: 上表の全機構が無効のまま。

### Option B: `raise` のみ (DLQ なし)
- リトライは発動するが、3 回 (初回+2リトライ) 全滅するとイベントが**消失**する。
- 再処理経路がゼロのため却下。

### Option C: `raise` + EventInvokeConfig (retry=2) + OnFailure → SQS DLQ (採用)
- 一過性障害は自動リトライで回復、恒久障害はイベントが DLQ に 14 日保全される。
- AWS 標準機構のみで構成でき、アプリコードは `raise` 1 行で済む。

### Option D: アプリ内で自前の SQS 送信 (try/except で boto3 送信)
- AWS 標準機構の再発明。送信自体の失敗・IAM 複雑化・テスト負荷が増える。却下。

## 4. 決定 (Decision)

1. **handler**: 構造化 ERROR ログ (JSON 1 行: `level` / `error_type` /
   `error_message` / `event_id` / `fetched_at`) を出力した後、例外を `raise` する。
2. **template.yaml**: `EventInvokeConfig` を追加 —
   `MaximumRetryAttempts: 2`, `MaximumEventAgeInSeconds: 3600`,
   `DestinationConfig.OnFailure` → SQS DLQ。
3. **SQS DLQ**: `MessageRetentionPeriod: 1209600` (14 日 = 最大値。週末障害でも
   月曜の営業時間に間に合う), SSE 有効。
4. **IAM**: 実行ロールに対象 DLQ への `sqs:SendMessage` のみ付与 (最小権限)。
5. `print(json.dumps(...))` は **PR #3 (Lambda Powertools Logger) までの暫定橋渡し**
   であり、恒久実装ではない。出口をコードコメントと本 ADR の両方に明記する。

### リトライ × 冪等性の相互作用

リトライによる再実行では `event['time']` が不変であり、各記事の S3 キーは
`SHA-256(link|published)` から決定的に導出される (ADR-002)。したがって
**再実行は同一キーへの上書きに収束し、重複書き込みは構造的に発生しない**。

## 5. 結果 (Consequences)

### 5.1 ポジティブ
- 障害が `AWS/Lambda` Errors メトリクスとして**可視化**される
- 一過性障害 (ネットワーク瞬断等) は自動リトライで**自己回復**する
- 恒久障害でもイベントが DLQ に 14 日保全され、**再処理 (re-drive) が可能**になる
- ERROR ログが JSON 構造化され、CloudWatch Logs Insights で
  `filter level = "ERROR" | stats count() by error_type` が可能になる

### 5.2 ネガティブ / 残課題
- DLQ は放置すると「静かに溜まる」— **深さ監視の Alarm と Re-drive Runbook は
  PR #4 のスコープ**として明示的に残す
- `print` ベースの構造化ログは暫定 — **PR #3 で Powertools Logger に置換**
- `feedparser.parse` 自体のタイムアウト/内部リトライは未実装 — **PR #5 のスコープ**

## 6. 教訓 (Lessons Learned)

1. **async Lambda の成否は return 値ではなく例外で決まる**: `statusCode` 慣習の
   無批判な流用は、API Gateway から event-driven への移植時の典型事故である。
2. **リトライを安全にするのは冪等性である**: 機能追加の順序 (PR #1 → PR #2) は
   それ自体がアーキテクチャ判断であり、ADR に残す価値がある。
3. **「例外を握る」場所は層によって戦略が異なる**: レコード単位の異常は分岐して
   隔離 (GCP 側 #57 の TaggedOutput DLQ と同思想)、実行単位の異常は伝播させて
   プラットフォームの retry/DLQ に委ねる。
4. **暫定実装には出口を刻む**: 「いつ・何で置き換えるか」(print → PR #3 Powertools)
   を PR 番号付きでコードと ADR の両方に明記し、負債の迷子を防ぐ。

## 7. 参考資料

- [AWS Lambda asynchronous invocation](https://docs.aws.amazon.com/lambda/latest/dg/invocation-async.html) — リトライ回数・成否判定の公式仕様
- [Lambda destinations (OnFailure)](https://docs.aws.amazon.com/lambda/latest/dg/invocation-async.html#invocation-async-destinations) — DLQ より新しい失敗先設定
- [AWS SAM: EventInvokeConfig](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/sam-property-function-eventinvokeconfiguration.html)
- ADR-002 (本リポジトリ) — リトライ安全性の前提となる冪等キー設計