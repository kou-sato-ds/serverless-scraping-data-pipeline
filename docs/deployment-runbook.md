# Phase B-3 Deployment Runbook

> 5/16 (土) の Athena 分析層デプロイ手順。未来の自分への手紙。
> 所要時間目安: 60〜90分。AWS課金: 数十円未満。

## 🎯 ゴール

- `sam deploy` で Glue Database / Crawler / Athena WorkGroup を本番反映
- Crawler を手動実行し、Athena テーブルを認識させる
- `docs/analysis/` の SQL 3本を実行し、結果スクリーンショットを取得
- 各 analysis.md に実測値と所見を追記

## 📋 事前チェック (デプロイ前 5分)

```bash
cd ~/projects/serverless-scraping-data-pipeline
source .venv/bin/activate

# 1. ローカルテストが通ること
pytest tests/ -v --cov=src --cov-fail-under=90
# 期待: 24 passed

# 2. SAM template が valid であること
sam validate --lint
# 期待: "is a valid SAM Template"

# 3. AWS 認証情報が有効か
aws sts get-caller-identity
# 期待: Account ID と User ARN が表示される

# 4. S3 にデータが溜まっているか (重要)
aws s3 ls s3://cloudpro-news-rss-$(aws sts get-caller-identity --query Account --output text)-ap-northeast-1/google-news-rss/ --recursive | tail -5
# 期待: year=2026/month=05/... のパスでファイルが見える
```

## 🚀 デプロイ (10分)

```bash
sam build
sam deploy
# Stack 名は既存と同じ: cloudpro-news-rss
# 既存スタックを上書き更新するので新規パラメータ入力は不要のはず
```

CloudFormation コンソールで `cloudpro-news-rss` スタックを開き、新リソース4つが
CREATE_COMPLETE になっていることを確認:
- NewsGlueDatabase
- GlueCrawlerRole
- NewsCrawler
- NewsAthenaWorkgroup

## 🔍 Crawler 実行 (5分)

```bash
# Crawler 起動
aws glue start-crawler --name cloudpro-news-rss-crawler

# 状態確認 (RUNNING → STOPPING → READY)
aws glue get-crawler --name cloudpro-news-rss-crawler \
  --query 'Crawler.State' --output text
# 数十秒〜数分で READY になる

# 作成されたテーブルを確認
aws glue get-tables --database-name cloudpro_news_rss_db \
  --query 'TableList[].Name'
# 期待: ["google_news_rss"]
```

## 🧪 Athena で初SQL (15分)

1. AWS コンソール → Athena → WorkGroup を `cloudpro-news-rss-workgroup` に切替
2. Database を `cloudpro_news_rss_db` に切替
3. `docs/analysis/01-source-ranking.md` の SQL を貼って実行
4. 結果が出たら **スクリーンショット保存** → `docs/analysis/screenshots/01-result.png` 等
5. 02, 03 も同様

## 📝 SQL 結果を docs/analysis/ に追記 (20分)

各 analysis.md の **"デプロイ後の実測値はここに追記予定"** セクションに:
- スクリーンショット (相対パス)
- TOP 5 程度の結果テーブル (markdown 形式)
- 2〜3文の所見 (例:「Yahoo!ニュースが全体の○%を占めており、媒体の集中度が高い」)

## ✅ 完了後のコミット

```bash
git add docs/analysis/
git commit -m "feat(analytics): Add Athena query results and insights for Phase B-3"
git push
```

## 🧹 (任意) コスト確認

```bash
# Athena WorkGroup のスキャン量確認
aws athena get-work-group --work-group cloudpro-news-rss-workgroup \
  --query 'WorkGroup.Configuration'
```

スキャン量が GB 未満であれば数十円未満で済むはず。

## 🆘 トラブル対応

| 症状 | 対処 |
|---|---|
| `sam deploy` で IAM Role 名重複エラー | 既存スタックを `sam delete` して再作成 |
| Crawler 実行後にテーブルが作られない | S3 にデータが無い → Lambda の動作確認 |
| Athena クエリで HIVE_BAD_DATA エラー | JSON 構造の問題 → 1ファイルを `aws s3 cp` で取得し中身確認 |
| Athena クエリが遅い | パーティションフィルタ (`WHERE year='2026'`) を確実に書く |
