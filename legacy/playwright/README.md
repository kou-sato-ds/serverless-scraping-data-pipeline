# Legacy: Playwright版 Google News Scraper

> このディレクトリは **歴史的経緯の保存** を目的としており、**本番運用は行いません**。
> 現役のコードはリポジトリのルートにある RSS 版を参照してください。

## なぜ残しているのか

このプロジェクトは当初、**AWS Lambda Container Image + Playwright** による
スクレイピング基盤として実装されました。実装は以下の理由で **完成・成功** しています:

- ✅ Container Image (約 700MB) を Lambda にデプロイし、Chromium 起動を確認
- ✅ EventBridge スケジュールで自動実行され、S3 への JSON 保存も完了
- ✅ IAM 最小権限・SSE-S3 暗号化・90日ライフサイクルなど本番品質の設計

しかし**実運用評価フェーズ**で以下の課題が判明しました:

| 課題 | 詳細 |
|---|---|
| **データ品質** | `<article>` セレクタがサイドバーのナビゲーション要素 (「トップニュース」「ウェザーニュース」) を拾い、実記事が取得できないケースがあった |
| **実行時間** | コールドスタート + Chromium 起動で 30〜60 秒。RSS なら 1〜2 秒 |
| **コスト** | Memory 2048MB × 60秒 × 1時間ごと → RSS 版の約 30 倍 |
| **保守性** | Google News の DOM 構造変更に脆弱。難読化クラス名の追従コストが高い |

これらを踏まえ、**TCO (総保有コスト) 評価の結果として RSS 版に移行** しました。
詳細な意思決定の経緯は `../docs/ADR-001-playwright-to-rss.md` (Phase B-2 で作成予定)
を参照してください。

## このディレクトリの位置づけ

- ❌ **本番デプロイ対象ではない**
- ✅ **「重い依存ライブラリを Lambda で動かす技術力」の実証として保存**
- ✅ **技術選定の根拠を語る際の比較対象として参照**

## 当時のスタック

| 項目 | 内容 |
|---|---|
| デプロイ方式 | Lambda Container Image (PackageType: Image) |
| ベースイメージ | `python:3.9-slim` + `awslambdaric` |
| ブラウザ | Chromium (via Playwright) |
| メモリ / タイムアウト | 2048 MB / 300 秒 |
| アーキテクチャ | x86_64 |

## ファイル構成

```
legacy/playwright/
├── README.md          # このファイル
├── Dockerfile         # Container Image 定義 (Playwright + Chromium)
├── requirements.txt   # playwright + boto3
├── template.yaml      # SAM テンプレート (Image type)
└── src/
    └── app.py         # Playwright スクレイピングロジック
```

## このコードを動かしたい場合 (アーカイブ目的)

```bash
cd legacy/playwright
sam build
sam deploy --guided  # スタック名を別にすること推奨 (例: cloudpro-news-scraper-legacy)
```

> **注意**: 本番系 (RSS 版) と同じ S3 バケット名を生成しないよう、
> `--parameter-overrides ProjectName=cloudpro-news-scraper-legacy` を付けてください。
