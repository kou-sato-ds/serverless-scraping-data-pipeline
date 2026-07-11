# ADR-002: S3 オブジェクトキーを Content-Addressable 設計に変更する (記事粒度ハッシュ)

| 項目 | 内容 |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-05-11 |
| **Decision Makers** | プロジェクトオーナー |
| **関連 Issue / PR** | PR #1 (idempotent-keys) |
| **Supersedes** | ADR-001 のキー設計部分 (`hour=HH/HHMMSS.json`) |

---

## 1. 背景 (Context)

ADR-001 にて Playwright から RSS への移行が完了し、本パイプラインは Hive 互換パーティション (`year=/month=/day=/hour=`) でデータレイクを構築する設計となった。

v1.0 時点での S3 キー生成ロジックは以下のとおり (`src/app.py` の旧 `build_object_key`):

```python
def upload_to_s3(articles):
    now = datetime.now(timezone.utc)          # ← Lambda 実行時の壁時計
    object_key = f"{S3_PREFIX}/year={now.year}/.../{now.strftime('%H%M%S')}.json"
    s3.put_object(Bucket=..., Key=object_key, Body=...)
```

特徴:

- **1 Lambda 実行 = 1 S3 オブジェクト** (記事配列を1ファイルにまとめる)
- **ファイル名は Lambda 実行時刻** (`HHMMSS.json`)
- **パーティションも Lambda 実行時刻ベース** (`year=`〜`hour=`)

実装は完成し、テスト 24 件もパスしている。**しかし運用シミュレーションの結果、データ整合性に関わる致命的欠陥が検出**されたため、本 ADR でキー設計の全面刷新を決定する。

---

## 2. 検出した問題 (Problem)

### 2.1 実行粒度・記事粒度の双方で冪等性が破綻している (致命的)

#### シナリオ A: EventBridge → Lambda の自動リトライ

EventBridge Scheduled Rule で起動された Lambda は、**async invocation** モードとなる。このモードでは Lambda 側のデフォルトで「失敗時に最大 2 回までリトライ」が設定されている。

S3 PutObject 直前にネットワーク瞬断 → Lambda が例外で終了したケースを考える:

| 試行 | `datetime.now()` の値 | 生成される S3 キー |
|---|---|---|
| 1 回目 (失敗) | 03:00:01 | `.../hour=03/030001.json` (書込み未完了) |
| 2 回目 (リトライ) | 03:00:14 | `.../hour=03/**030014.json**` ← **別キー** |

結果: **同じ記事リストが S3 上に重複して書き込まれる**。Glue Crawler は両方をスキャンし、Athena クエリは `SELECT COUNT(*)` で**実態の2倍を返す**。

#### シナリオ B: 同一記事の再取得 (記事粒度の重複)

Google News RSS は毎時 100 件の最新記事を返す。**ある記事は数時間にわたって RSS フィードに残り続ける**。すなわち:

| 実行時刻 | フィード内に「記事 X」が存在 | キー (旧設計) |
|---|---|---|
| 03:00 | あり | `hour=03/030000.json` (中に記事 X) |
| 04:00 | あり | `hour=04/040000.json` (中に記事 X) |
| 05:00 | あり | `hour=05/050000.json` (中に記事 X) |

結果: **同じ記事 X が 3 つの異なるオブジェクトに含まれる**。`SELECT COUNT(DISTINCT link) FROM news_articles` を毎回手作業で書かない限り、ダッシュボードの記事数は偽の値を返す。

### 2.2 サイレント失敗の構造的リスク

両シナリオとも **Lambda は正常終了**しており、CloudWatch メトリクスや Errors カウントには異常が出ない。ADR-001 の教訓 1 (「動いた」と「正しい」は違う) と完全に同じ構造の問題が、データレイヤーで再発している。

### 2.3 Late-arriving Data への耐性なし

旧設計はパーティションを**取得時刻 (fetched_at) ベース**で切るため、深夜 23:59 に取得された 12:00 公開の記事は `hour=23` パーティションに入る。本来は `hour=12` に属するべきデータが時刻またぎで散逸し、`WHERE day='X' AND hour='12'` という自然なクエリで取り漏らしが発生する。

---

## 3. 検討した選択肢 (Options Considered)

### Option A: `event['time']` を S3 キーの源泉にする

EventBridge Scheduled Rule のイベントペイロードには `time` フィールドがあり、リトライ時も同一値が渡される。これを `build_object_key` の入力にすれば、シナリオ A (実行リトライ) は解決する。

- **メリット**: 実装が極めて軽量 (1 関数の引数変更のみ). ファイル数は時間あたり1個に抑えられ Small Files Problem 無し
- **デメリット**: **シナリオ B (記事粒度の重複) は解決しない**. Late-arriving data 耐性も無い

### Option B: `event['id']` を S3 キーの源泉にする

イベントごとに一意な UUID であり、決定的でリトライにも強い。

- **メリット**: 一意性が保証される
- **デメリット**: 時刻情報が欠落するためパーティション分割と相性が悪い. キーが UUID なので人間可読性ゼロ. シナリオ B も未解決

### Option C: 記事粒度の Content-Addressable Storage (採用)

各記事に対し `SHA-256(link + published)[:16]` を計算し、これをファイル名にする。1 記事 = 1 S3 オブジェクト. パーティションは記事の `published_at` から導出する.

- **メリット**:
  - シナリオ A・B の両方を**構造的に**解決 (上書きセマンティクスで重複を許容しない)
  - Late-arriving data も自然に正しいパーティションに収まる
  - 「データ品質を構造で保証する」というデータエンジニアリングの本道
- **デメリット**:
  - S3 オブジェクト数が増加 (Small Files Problem の温床)
  - 実装複雑度が上昇 (PR1 のスコープが拡大)
  - Lambda 内の PutObject 回数が増加 (シーケンシャルだと数秒延びる)

### Option D: 「現状維持 + Athena 側で DISTINCT で握りつぶす」

データレイクには重複を入れたまま、クエリ側で `DISTINCT link` する運用。

- **メリット**: コード変更ゼロ
- **デメリット**: **データレイクが「真実の単一の源 (Single Source of Truth)」でなくなる**. クエリ作成者全員が DISTINCT を書く規律を強いられ、忘れた瞬間にダッシュボードが壊れる. **データエンジニアリングの根本思想に反する**ため不採用

---

## 4. 決定 (Decision)

**Option C: 記事粒度の Content-Addressable Storage** を採用する。

### 4.1 キーの構造

```
<S3_PREFIX>/year=YYYY/month=MM/day=DD/hour=HH/<sha256(link|published)[:16]>.json
```

実例:
```
google-news-rss/year=2026/month=05/day=09/hour=13/a3f5b9c8e1d2f4a6.json
```

### 4.2 ハッシュの源泉

| 入力 | 役割 |
|---|---|
| `link` (記事 URL) | 一次的な一意識別子 |
| `published` (ISO 8601 文字列) | 同一 URL の更新版を別記事として扱うための補助 |

`SHA-256(f"{link}|{published}")` の先頭 16 文字 (64bit) を採用. 誕生日問題では **約 40 億記事で衝突確率 ~10^-6**, 本パイプラインの想定スケール (月数千件) では衝突は実質ゼロ.

### 4.3 パーティション時刻の決定ロジック

優先順位:

1. 記事の `published` (ISO 8601) があればそれを使用
2. 無ければ `fetched_at` (EventBridge `event['time']`) を使用

Late-arriving data も含めて、データの「論理的所属」が一意に決まる.

### 4.4 ペイロードスキーマ (v2.0)

```json
{
  "schema_version": "2.0",
  "fetched_at": "2026-05-11T03:00:00+00:00",
  "source_feed": "https://news.google.com/rss?...",
  "article_hash": "a3f5b9c8e1d2f4a6",
  "article": {
    "title": "...",
    "link": "...",
    "published": "...",
    "source": "..."
  }
}
```

v1.0 との互換性は破壊する. `legacy/` への退避は不要 (v1.0 で書かれた S3 オブジェクトは Lifecycle 90 日で自然消滅する設計のため).

### 4.5 `fetched_at` の決定

旧実装の `datetime.now(timezone.utc)` を撤廃し、`event['time']` をパースして使用する. EventBridge は同一イベントに対し同一の `time` を渡すため、リトライ時も値が変わらない. これが冪等性の基盤.

---

## 5. 結果 (Consequences)

### 5.1 ポジティブ

- **構造的冪等性**: シナリオ A・B の両方が S3 の上書きセマンティクスにより自動解決. アプリケーションロジック側に「dedup」概念が一切登場しない (最も保守しやすい状態)
- **Late-arriving data 耐性**: パーティションが `published_at` ベースのため、深夜またぎや時刻ズレの取得でもデータが正しい場所に着地する
- **Audit trail 保持**: `fetched_at` は payload にメタデータとして残るため、「いつ取得されたか」と「いつ公開されたか」を両方追跡可能
- **Athena クエリの素直さ**: `SELECT COUNT(*) FROM news_articles` がそのまま正しい記事数を返す. クエリ作成者に DISTINCT 規律を強いない
- **テスト容易性**: 全ロジックが決定的関数 (同じ入力 → 同じ出力) のため、`datetime.now()` を mock する必要が無い

### 5.2 ネガティブ / トレードオフ

#### Small Files Problem の悪化

| 指標 | 旧 (v1.0) | 新 (v2.0) | 増加率 |
|---|---|---|---|
| S3 オブジェクト数 / 日 | 24 | 最大 2,400 (24 × 100) | 100倍 |
| S3 オブジェクト数 / 年 | 8,760 | 最大 876,000 | 100倍 |
| Athena クエリで読む小ファイル数 | 少 | 多 | クエリレイテンシ増 |

**対処策**:

| 期間 | 対処 |
|---|---|
| **短期** | S3 Lifecycle で 90 日後に削除 (既存実装で対応済) |
| **中期** (PR #6 で予定) | 日次バッチで Athena CTAS により Parquet に圧縮統合. `compacted/year=/month=/day=/data.parquet` に集約し、生 JSON は別 prefix にアーカイブ |
| **長期** | Apache Iceberg テーブル化. `MERGE INTO` で Upsert 実装し、生ファイル蓄積を不要に |

#### Lambda 実行時間の増加

- 旧: PutObject × 1 回
- 新: PutObject × N 回 (N = 記事数, 最大 100)

実測想定: 1 回あたり 50ms → 100 記事で +5 秒. Lambda timeout (30 秒) には余裕あり.

**将来の最適化** (PR #5 で検討予定): `concurrent.futures.ThreadPoolExecutor` での並列化. ただし PR1 のスコープを膨らませないため、現時点ではシーケンシャル実装.

#### IAM ポリシーへの影響なし

旧: `Resource: arn:aws:s3:::bucket/${S3DataPrefix}/*` (ワイルドカード)
新: 同上. プレフィックス配下で深くなるだけのため、既存ポリシーで動作する.

### 5.3 中立 / 将来的な含意

- 本 ADR の決定により、本パイプラインは **CAS (Content-Addressable Storage)** + **Append-only data lake** のパターンに準拠することになった. これは Kafka の "Idempotent Producer" や Iceberg の "Equality Delete" と同じ思想であり、将来的なストリーミング基盤への発展余地が生まれる
- スキーマバージョン (`schema_version`) を payload に含めたため、将来のスキーマ進化時に「v2.0 と v3.0 を分けて読む」ことが可能

---

## 6. 教訓 (Lessons Learned)

1. **「実行の冪等性」と「データの冪等性」は別物である**: シナリオ A だけ見ると Option A (event['time']) で十分に見える. しかしデータレイクの真の冪等性 (シナリオ B) を要求すると、データ自身からキーを導出する CAS パターンに必然的に行き着く. データエンジニアリングでは常に**データ粒度で**冪等性を問う癖を持つこと.

2. **`datetime.now()` はデータパイプラインの敵**: 壁時計を関数内で呼ぶ実装は「同じ入力からは同じ出力」という決定性を破壊し、テストで `freeze_time` 等の mock を必要とし、リトライ安全性を壊す. **時刻は常に "外から渡される値" として扱う**.

3. **トレードオフは隠さず ADR に明記する**: 本 ADR は Small Files Problem という新たな技術的負債を意図的に受け入れている. これを「採用しなかった代替案」と「対処の3段ロードマップ」として明示することで、後任者 (または将来の自分) が「なぜこの設計を選んだか」と「どう発展させるべきか」を一目で理解できる. **完璧な設計より、トレードオフを言語化できる設計**を選ぶこと.

4. **テストで冪等性を契約として強制する**: PR1 では `TestIdempotency::test_same_event_produces_same_keys` を追加し、「同じイベントを2回流したら S3 のオブジェクト数が変わらない」ことをテストで保証する. このテストが緑である限り、将来誰がコードを触っても冪等性は壊せない. **設計思想はコメントではなくテストで守る**.

---

## 7. 参考資料

- [Idempotent Producer (Kafka)](https://kafka.apache.org/documentation/#producerconfigs_enable.idempotence) — 「データ粒度の冪等性」の業界標準的な実装思想
- [Content-Addressable Storage (Wikipedia)](https://en.wikipedia.org/wiki/Content-addressable_storage) — Git の object storage が代表例
- [AWS Lambda Async Invocation & Retry Behavior](https://docs.aws.amazon.com/lambda/latest/dg/invocation-async.html) — Lambda の async モードでのリトライ挙動
- [EventBridge Event Structure](https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-events-structure.html) — `time` / `id` フィールドの公式仕様
- [Athena Small Files Problem](https://aws.amazon.com/blogs/big-data/top-10-performance-tuning-tips-for-amazon-athena/) — 「Tip #4: Optimize file sizes」が本 ADR の中期対処策の根拠
- [Apache Iceberg: Row-level Operations](https://iceberg.apache.org/docs/latest/spark-writes/) — 長期対処として参照する Upsert 設計