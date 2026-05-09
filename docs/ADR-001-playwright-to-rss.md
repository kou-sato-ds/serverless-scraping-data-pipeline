# ADR-001: Google News 取得方式を Playwright スクレイピングから公式 RSS に移行する

| 項目 | 内容 |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-05-09 |
| **Decision Makers** | プロジェクトオーナー |
| **関連 Issue / PR** | (本リポジトリの初期実装) |

---

## 1. 背景 (Context)

本プロジェクトは「Google News から記事を1時間ごとに収集し、構造化データとして S3 に蓄積する」ことを目的とするサーバーレスデータパイプラインである。

初期実装では、**ヘッドレスブラウザ自動操作 (Playwright + Chromium) を AWS Lambda の Container Image で動かす方式**を採用した。これは以下の理由から自然な選択だった:

- Google News は SPA (Single Page Application) であり、JavaScript レンダリングが必要だと想定された
- Container Image 方式により Lambda の 250MB ZIP 制限を回避し、Playwright (約 700MB) を持ち込める
- 「重い依存ライブラリを Lambda に同梱できる技術力」の実証として価値がある

実装は完成し、本番稼働まで到達した。**しかし運用評価フェーズで重大な品質問題が発覚** したため、本 ADR で代替方式への移行を決定する。

---

## 2. 検出した問題 (Problem)

### 2.1 データ品質の問題 (致命的)

Lambda 実行で `statusCode: 200`, `count: 3` が返り、S3 にも JSON が保存されたため、当初は「成功」と判定された。しかし**保存された JSON の中身を精査** したところ、以下が判明した:

```json
{
  "count": 3,
  "articles": [
    {"title": "トップニュース",   "link": ".../topics/CAAqJggK..."},  // カテゴリページ
    {"title": "ウェザーニュース", "link": "https://weathernews.jp/onebox/..."},  // 天気ウィジェット
    {"title": "日本",             "link": ".../topics/CAAqIQgK..."}   // カテゴリページ
  ]
}
```

**取得していたのは記事ではなく、サイドバーのナビゲーション要素である**。`<article>` セレクタが Google News の SPA 構造上、メイン記事領域だけでなく付随的なナビゲーション領域も拾ってしまう構造的な問題。

これは「動いた = 成功」ではないことを示す典型例で、**データエンジニアリングにおいて最も避けるべきサイレント失敗** に該当する。

### 2.2 構造的脆弱性

- Google News の DOM クラス名 (`JtKRv`, `niO9ze` 等) は**自動生成された難読化文字列** であり、数日〜数週間で変動する
- セレクタの追従コストが慢性的に発生する見込み
- データ品質の検証なしには、また同じサイレント失敗を繰り返すリスクがある

### 2.3 高い実行コスト

| 指標 | 実測値 |
|---|---|
| Lambda Memory Size | 2,048 MB |
| Lambda Duration | 27,578 ms |
| Lambda Init Duration | 1,525 ms |
| Lambda 1回あたり消費 | 約 55 GB-秒 |
| Container Image サイズ | 約 700 MB |
| デプロイ時間 (ECR push 込) | 5〜10 分 |

ヘッドレスブラウザの起動オーバーヘッドが圧倒的に支配的で、得られるデータ量 (タイトル + リンク × 3件) との比率が著しく悪い。

---

## 3. 検討した選択肢 (Options Considered)

### Option A: Playwright のセレクタを改善し続ける

- **メリット**: 既存実装を活かせる、任意のサイトに応用可能
- **デメリット**: DOM 変動で定期的に壊れる、Bot 検知のリスク、コスト構造が変わらない、本質的にデータ品質保証が困難

### Option B: Google News 公式 RSS フィードに移行 (採用)

- Google News は公式 RSS を提供している (`https://news.google.com/rss?hl=ja&gl=JP&ceid=JP:ja`)
- XML 形式の構造化データ。スキーマが安定している
- 認証不要、利用規約上も問題なし
- **メリット**: 軽量、安価、安定、メタデータが豊富 (公開日時・発行元媒体)
- **デメリット**: 「任意サイトのスクレイピング」汎用性は失う

### Option C: Web スクレイピング外注 / 商用 API 利用

- **メリット**: 自前運用不要
- **デメリット**: 月額固定費発生、ベンダーロックイン、本プロジェクトの学習目的と不整合

---

## 4. 決定 (Decision)

**Option B: Google News 公式 RSS への完全移行** を採用する。

具体的には:

| 観点 | 旧 (Playwright) | 新 (RSS) |
|---|---|---|
| 取得方式 | Chromium 経由でレンダリング後 DOM パース | `feedparser` で XML パース |
| デプロイ方式 | Container Image (PackageType: Image) | ZIP (PackageType: Zip) |
| ランタイム | Python 3.9 (slim + awslambdaric) | Python 3.12 (Lambda native) |
| アーキテクチャ | x86_64 | **arm64 (Graviton2)** で約20%コスト削減 |
| Memory / Timeout | 2048 MB / 300 秒 | **256 MB / 30 秒** |
| 依存ライブラリ | playwright + chromium + 20以上の OS ライブラリ | `feedparser` + `boto3` のみ |
| メタデータ | title, link | + `published`, `source` (発行元媒体) |
| 出力形式 | フラット | **Hive 互換パーティション** (`year=/month=/day=/hour=`) |

旧実装は `legacy/playwright/` 配下にアーカイブし、本意思決定の比較対象として保存する。

---

## 5. 結果 (Consequences)

### 5.1 ポジティブ (実測値)

本番デプロイ後の Lambda CloudWatch メトリクスから取得した実測比較:

| 指標 | Playwright 版 | RSS 版 | 削減 / 改善 |
|---|---|---|---|
| 実記事取得数 | 0 件 (ナビ項目3件のみ) | **30 件** (実ニュース) | データ品質 ∞倍 |
| 出力サイズ | 759 B | 14,458 B | **19倍** の情報量 |
| Duration | 27,578 ms | 1,778 ms | **-94%** |
| Init Duration | 1,525 ms | 717 ms | **-53%** |
| Memory Size | 2,048 MB | 256 MB | **-87.5%** |
| Max Memory Used | 652 MB | 93 MB | -86% |
| GB-秒 / 月 (24回/日 × 30日想定) | 約 39,700 | 約 320 | **-99.2%** |
| デプロイ時間 | 5〜10 分 | 約 30 秒 | **-95%** |
| 必要な OS 依存ライブラリ | 20+ | 0 | — |
| メタデータ項目 | 2 (title, link) | 4 (title, link, published, source) | +2 |

### 5.2 ネガティブ / トレードオフ

- **任意サイトのスクレイピング汎用性は失う**: 本プロジェクトの目的 (Google News ニュース収集) には不要なため、許容可能なトレードオフと判断
- **Google News RSS への依存**: フィード仕様変更や提供停止のリスクは残るが、Google が維持している公式仕様であり安定性は DOM スクレイピングより遥かに高い

### 5.3 中立 / 将来的な含意

- **Hive 互換パーティション** にしたことで、後段の Athena / Glue Crawler / QuickSight 連携が低コストで実現可能になる (Phase B-3 で着手予定)
- 旧 Playwright 実装は `legacy/playwright/` に残っているため、将来「Google News 以外の任意サイトを対象にしたい」要件が出た場合、ベースとして再利用できる

---

## 6. 教訓 (Lessons Learned)

1. **「動いた」と「正しい」は違う**: ステータス 200 とオブジェクトが S3 に作られた時点で成功と誤認しかけた。**データ品質の検証ステップ** (中身を実際に開いて読む) が、データエンジニアリングでは何より重要である
2. **TCO で語る**: 「Playwright が動かない」ではなく「Playwright も動くが TCO で RSS が圧倒的に有利」という枠組みで決断したことで、技術選定の妥当性を**実数値で語れる**ようになった
3. **アーカイブは捨てない**: 旧実装を `legacy/` に残すことで、本 ADR の比較対象として価値が生まれている。Git 履歴に残るだけでなく、ツリー上にも残すことで意思決定の証跡が強固になる
4. **規約に従う**: SAM ZIP ビルドでは `requirements.txt` を `CodeUri` 直下に置く必要がある。フレームワークごとの「依存定義の置き場所」規約を外すと、依存がサイレントにスキップされる (本プロジェクトでも一度遭遇)

---

## 7. 参考資料

- [Google News RSS feed format](https://news.google.com/rss)
- [feedparser ドキュメント](https://feedparser.readthedocs.io/)
- [AWS Lambda Pricing (ARM/Graviton2)](https://aws.amazon.com/lambda/pricing/)
- [Hive-style partitioning for Athena](https://docs.aws.amazon.com/athena/latest/ug/partitions.html)
- [AWS SAM `AWS::Serverless::Function` (Zip type)](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/sam-resource-function.html)