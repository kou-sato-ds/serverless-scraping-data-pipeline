# Analysis 01: 媒体別の記事流量ランキング

## 🎯 ビジネスの問い
> 「Google News に最も多く記事を流している媒体トップ10はどこか?」
> 「どの媒体が"今"発信力を持っているか?」

## 📊 想定読者
- メディアリレーション・PR 担当
- 市場調査・競合分析担当
- 媒体ごとの広告出稿戦略を立てる人

## 🔍 SQL クエリ

\`\`\`sql
SELECT
    article.source AS media_name,
    COUNT(*) AS article_count
FROM google_news_rss,
     UNNEST(articles) AS t(article)
WHERE year = '2026' AND month = '05'
GROUP BY article.source
ORDER BY article_count DESC
LIMIT 10;
\`\`\`

## 📈 期待される結果スキーマ

| media_name | article_count |
|---|---|
| Yahoo!ニュース | 1,234 |
| 産経ニュース | 987 |
| BBC | 654 |
| ... | ... |

## 💡 得られる洞察例
- 特定媒体への記事集中度 (上位3媒体で全体の何%か)
- 新興 vs 老舗メディアの存在感比較
- 月別での順位変動を追えば「**話題が動いた瞬間**」を捕捉可能

## 📌 デプロイ後の実測値はここに追記予定
(Athena コンソールのクエリ結果スクリーンショット + 短い所見を貼る)
